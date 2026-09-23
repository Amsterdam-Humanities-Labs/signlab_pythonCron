#!/usr/bin/env python3
"""
Server Monitor - Runs every 5 minutes to check:
1. Disk usage (alert when < 10% free)
2. Rclone mount health (test write/read to sc_test)
3. MySQL connectivity (30s timeout)

Sends Discord alerts on failures.
"""

import os
import sys
import time
import logging
import subprocess
import socket
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sc_paths import sc_path
# Shared checks and alerting: the installed package, else the copy vendored
# beside this script (see README.md).
try:
    from signlab_client_monitor import disk_usage, mount_read_write, send_alert as _send
except ImportError:
    from python_client import disk_usage, mount_read_write, send_alert as _send

# ---------------------------------------------------------------------------
# Load .env from <root>/zin/.env, normally /web/zin/.env
# ---------------------------------------------------------------------------
ENV_FILE = sc_path("zin", ".env")
if Path(ENV_FILE).exists():
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Discord credentials (DISCORD_WEBHOOK_URL, or DISCORD_BOT_TOKEN +
# DISCORD_CHANNEL_ID) are read from the environment by send_alert.
MOUNT_TEST_DIR = sc_path("media", "studioFiles", "sc_test")
DISK_THRESHOLD_PERCENT = 10  # alert when free space below this
SQL_TIMEOUT_SECONDS = 30
CHECK_INTERVAL_SECONDS = 300  # 5 minutes

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "user")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "admin_gebarenoverleg")

LOG_FILE = "/home/gomer/pythonCron/server_monitor.log"
HOSTNAME = socket.gethostname()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("server_monitor")

if not DB_PASSWORD:
    logger.warning(
        "DB_PASSWORD is not set; add it to %s or MySQL checks will fail.", ENV_FILE
    )

# ---------------------------------------------------------------------------
# Discord helper
# ---------------------------------------------------------------------------

ALERT_COOLDOWN_SECONDS = 3600  # re-notify a still-failing check at most hourly

_last_sent = {}  # alert title -> unix timestamp of last send


def send_alert(title: str, message: str, level: str = "error"):
    """Send a Discord notification, suppressing repeats within the cooldown."""
    now = time.time()
    last = _last_sent.get(title)
    if last is not None and now - last < ALERT_COOLDOWN_SECONDS:
        remaining = int(ALERT_COOLDOWN_SECONDS - (now - last))
        logger.info(f"Alert suppressed ({remaining}s of cooldown left): {title}")
        return
    _last_sent[title] = now

    if not _send(
        title,
        f"**Host:** {HOSTNAME}\n{message}",
        level=level,
        channels=("discord",),
        footer=f"Server Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        logger=logger,
    ):
        logger.warning(f"ALERT (no Discord): [{level}] {title} - {message}")


def clear_alert(title: str, healthy: bool):
    """Reset a check's cooldown and announce recovery once it passes again."""
    if not healthy or title not in _last_sent:
        return
    del _last_sent[title]
    if not _send(
        f"Recovered: {title}",
        f"**Host:** {HOSTNAME}\nCheck is passing again.",
        level="info",
        channels=("discord",),
        footer=f"Server Monitor | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        logger=logger,
    ):
        logger.info(f"RECOVERED (no Discord): {title}")


# ---------------------------------------------------------------------------
# Check 1: Disk usage
# ---------------------------------------------------------------------------

def check_disk_usage() -> bool:
    """Return True if disk is healthy (>= threshold free)."""
    try:
        usage = disk_usage("/")
        free_percent = usage["free_percent"]
        logger.info(f"Disk free: {free_percent:.1f}%")

        if free_percent < DISK_THRESHOLD_PERCENT:
            free_gb = usage["free"] / (1024 ** 3)
            total_gb = usage["total"] / (1024 ** 3)
            send_alert(
                "Low Disk Space",
                (
                    f"Free space is **{free_percent:.1f}%** (below {DISK_THRESHOLD_PERCENT}% threshold)\n"
                    f"Free: {free_gb:.1f} GB / Total: {total_gb:.1f} GB"
                ),
                level="error",
            )
            return False
        return True
    except Exception as e:
        logger.error(f"Disk check failed: {e}")
        send_alert("Disk Check Failed", str(e))
        return False


# ---------------------------------------------------------------------------
# Check 2: Rclone mount (write + read test)
# ---------------------------------------------------------------------------

MOUNT_POINT = sc_path("media", "studioFiles")


def check_rclone_mount() -> bool:
    """Verify rclone FUSE mount is active, then write/read test. Return True if OK."""
    try:
        # Mounted, then mkdir/write/read/delete sc_test/monitor_test.txt, each
        # step with a 15 s timeout; raises on the first step that fails.
        mount_read_write(MOUNT_POINT, MOUNT_TEST_DIR, timeout=15)

        logger.info("Rclone mount: write/read OK")
        return True

    except subprocess.TimeoutExpired:
        msg = "Rclone mount not responding (timeout on write/read test)"
        logger.error(msg)
        send_alert("Rclone Mount Timeout", msg)
        return False
    except Exception as e:
        msg = f"Rclone mount check failed: {e}"
        logger.error(msg)
        send_alert("Rclone Mount Error", str(e))
        return False


# ---------------------------------------------------------------------------
# Check 3: MySQL connectivity with 30s timeout
# ---------------------------------------------------------------------------

def check_mysql() -> bool:
    """Run a simple SQL query with a 30-second timeout. Return True if OK."""
    try:
        import mysql.connector

        start = time.time()
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            connection_timeout=SQL_TIMEOUT_SECONDS,
        )
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        elapsed = time.time() - start
        cursor.close()
        conn.close()

        logger.info(f"MySQL: OK ({elapsed:.2f}s)")
        return True

    except ImportError:
        # Fall back to command-line mysql client
        return _check_mysql_cli()
    except Exception as e:
        msg = f"MySQL check failed: {e}"
        logger.error(msg)
        send_alert("MySQL Connection Failed", str(e))
        return False


def _check_mysql_cli() -> bool:
    """Fallback: test MySQL via the mysql CLI with timeout."""
    try:
        result = subprocess.run(
            [
                "mysql",
                f"-h{DB_HOST}",
                f"-u{DB_USER}",
                f"-p{DB_PASSWORD}",
                DB_NAME,
                "-e",
                "SELECT 1;",
            ],
            capture_output=True,
            text=True,
            timeout=SQL_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            logger.info("MySQL (cli): OK")
            return True
        else:
            raise RuntimeError(result.stderr.strip())
    except subprocess.TimeoutExpired:
        msg = f"MySQL query timed out after {SQL_TIMEOUT_SECONDS}s"
        logger.error(msg)
        send_alert("MySQL Timeout", msg)
        return False
    except Exception as e:
        msg = f"MySQL check failed: {e}"
        logger.error(msg)
        send_alert("MySQL Connection Failed", str(e))
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_checks():
    """Run all checks once."""
    logger.info("--- Running server checks ---")
    disk_ok = check_disk_usage()
    mount_ok = check_rclone_mount()
    mysql_ok = check_mysql()

    clear_alert("Disk Space Low", disk_ok)
    clear_alert("Disk Check Failed", disk_ok)
    clear_alert("Rclone Mount Timeout", mount_ok)
    clear_alert("Rclone Mount Error", mount_ok)
    clear_alert("MySQL Connection Failed", mysql_ok)
    clear_alert("MySQL Timeout", mysql_ok)

    if disk_ok and mount_ok and mysql_ok:
        logger.info("All checks passed")
    else:
        failed = []
        if not disk_ok:
            failed.append("disk")
        if not mount_ok:
            failed.append("rclone")
        if not mysql_ok:
            failed.append("mysql")
        logger.warning(f"Failed checks: {', '.join(failed)}")


def main():
    logger.info(f"Server monitor starting (interval={CHECK_INTERVAL_SECONDS}s)")

    # Install mysql-connector-python if not present
    try:
        import mysql.connector  # noqa: F401
    except ImportError:
        logger.info("Installing mysql-connector-python...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "mysql-connector-python"],
            capture_output=True,
        )

    while True:
        try:
            run_checks()
        except Exception as e:
            logger.error(f"Unhandled error in check loop: {e}")
            try:
                send_alert("Monitor Error", f"Unhandled error: {e}")
            except Exception:
                pass

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
