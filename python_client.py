"""The client half of the client monitor: register, heartbeat, submit metrics.

One class, `ClientMonitor`, whose whole job is to tell `api.php` that the
script calling it is still alive. It is deliberately unable to break the script
it monitors: every method catches everything, every request has a timeout, and
nothing raises. A monitoring system that can kill the job it monitors is worse
than no monitoring system.

The signature is the union of the six copies this replaces, so that every
existing call site keeps working unchanged - see README.md for the table.

This file is also **vendored verbatim** as `python_client.py` next to the
scripts that cannot rely on the package being installed - there is no build
step anywhere in this estate, so `from python_client import ClientMonitor` has
to keep working on a host where nobody has run an installer. Those copies are
byte-identical to this one on purpose, which is what makes them checkable:

    diff python_client.py <(curl -fsSL https://raw.githubusercontent.com/\
Amsterdam-Humanities-Labs/signlab_client_monitor_api/main/client/signlab_client_monitor/client.py)

and refreshable by replacing the file with what that URL returns. Edit this
file, never a vendored copy; a vendored copy that differs is the bug this
package was written to end.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from datetime import datetime
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

import requests

#: Where the API lives. Hardcoded in every caller before this package existed,
#: so it is the default here rather than something each caller must remember.
DEFAULT_API_URL = "https://signcollect.nl/client_monitor_api/api.php"

#: Seconds to wait for the API before giving up. Four of the six copies grew
#: this independently after a hanging request stalled a cron job.
DEFAULT_TIMEOUT = 10

#: What the API assumes if a client does not say. Matches `api.php`.
DEFAULT_HEARTBEAT_INTERVAL = 3600

_LOG = logging.getLogger("signlab_client_monitor")


class Response(dict):
    """The API's JSON reply, which is falsy when the call did not succeed.

    The copies disagreed on what these methods return: three returned the
    response dict, two returned a bool. Since a plain dict is always truthy,
    `if monitor.send_heartbeat():` meant opposite things depending on which
    copy you had imported. This is a dict - so `result["data"]` still works -
    that is False unless the API said `success`, so the bool convention is
    also right. Nothing has to choose.
    """

    def __bool__(self) -> bool:
        return bool(self.get("success"))

    @property
    def errors(self) -> list:
        return list(self.get("errors") or [])


def _failure(message: str) -> Response:
    return Response({"success": False, "data": None, "errors": [message]})


def _ensure_visible(logger: logging.Logger) -> None:
    """Give the client a stderr handler if nothing else has configured one.

    The scripts that used the pythonCron copy print progress to stdout and cron
    mails or redirects it; they never call `logging.basicConfig`. Logging into
    a vacuum would silently lose those lines, so if no handler anywhere would
    receive our records, attach one. A caller that has configured logging - or
    that used `setup_rotating_logger` - keeps full control.
    """
    probe = logger
    while probe:
        if probe.handlers:
            return
        if not probe.propagate:
            break
        probe = probe.parent
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[ClientMonitor] %(message)s"))
    logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)


class ClientMonitor:
    """Reports one script's liveness to the client monitor API.

    Typical use, from a cron job that runs hourly::

        monitor = ClientMonitor(
            client_id="check-disk",
            client_name="Disk Space Monitor",
            description="Monitors disk space and alerts when it runs low",
            heartbeat_interval=3600,
        )
        try:
            ...
            monitor.send_heartbeat_with_stats("success", "all mounts healthy",
                                              {"free_gb": 412})
        except Exception as exc:
            monitor.send_heartbeat_with_stats("error", str(exc))

    Registration happens by itself, once, immediately before the first
    heartbeat - so a client appears in the dashboard without anyone having to
    remember a one-off setup step, and a script that calls `register()` itself
    does not register twice.
    """

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        client_id: Optional[str] = None,
        client_name: Optional[str] = None,
        description: str = "",
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        timeout: int = DEFAULT_TIMEOUT,
        auto_register: bool = True,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Args:
            api_url: The API endpoint. Defaults to production; every existing
                caller passes it explicitly, and one passes it positionally.
            client_id: Stable, unique identifier - the primary key in the API.
                Required, despite following an argument that has a default.
            client_name: Display name for the dashboard. Defaults to client_id.
            description: What this client does, shown in the dashboard.
            heartbeat_interval: How often this client intends to check in, in
                seconds. The API derives online/warning/offline from it.
            timeout: Seconds to wait for the API.
            auto_register: Register on the first heartbeat if not already
                registered. Set False if the client is registered elsewhere.
            logger: Where to log. Defaults to the package logger.
        """
        if not client_id:
            raise ValueError("ClientMonitor requires a client_id")

        self.api_url = api_url
        self.client_id = client_id
        self.client_name = client_name or client_id
        self.description = description
        self.heartbeat_interval = heartbeat_interval
        self.timeout = timeout
        self.auto_register = auto_register
        self.hostname = socket.gethostname()

        self.logger = logger or _LOG
        if logger is None:
            _ensure_visible(self.logger)

        self._registered = False

    # -- the API ---------------------------------------------------------

    def register(self, metadata: Optional[Dict[str, Any]] = None) -> Response:
        """Create this client in the monitoring system.

        Safe to call more than once and safe to call on every run: the API
        rejects a duplicate `client_id` as an error, which is exactly what an
        already-registered client looks like, so that particular error is
        reported as success.
        """
        payload = {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "description": self.description,
            "heartbeat_interval": self.heartbeat_interval,
            "metadata": metadata or {
                "hostname": self.hostname,
                "python_version": sys.version.split()[0],
                "registered_at": datetime.now().isoformat(),
            },
        }

        result = self._post("register", payload)
        if result:
            self._registered = True
            self.logger.info("registered: %s", self.client_id)
        elif any("already exists" in str(err).lower() for err in result.errors):
            self._registered = True
            self.logger.info("already registered: %s", self.client_id)
            return Response({"success": True, "data": result.get("data"), "errors": []})
        else:
            self.logger.warning("registration failed for %s: %s",
                                self.client_id, result.errors)
        return result

    def send_heartbeat(self, metadata: Optional[Dict[str, Any]] = None) -> Response:
        """Tell the API this client is alive, refreshing its `last_seen`."""
        self._register_if_needed()
        payload = {
            "client_id": self.client_id,
            "metadata": metadata or {
                "last_run": datetime.now().isoformat(),
                "hostname": self.hostname,
            },
        }

        result = self._post("heartbeat", payload)
        if result:
            self.logger.info("heartbeat sent: %s (status %s)", self.client_id,
                             (result.get("data") or {}).get("status", "unknown"))
        else:
            self.logger.warning("heartbeat failed for %s: %s",
                                self.client_id, result.errors)
        return result

    def send_heartbeat_with_stats(
        self,
        status: str,
        message: str,
        stats: Optional[Dict[str, Any]] = None,
    ) -> Response:
        """Heartbeat carrying the outcome of the run.

        `status` is free text by convention: `success`, `warning` or `error`.
        `stats` is merged into the metadata blob the dashboard displays.
        """
        metadata = {
            "last_run": datetime.now().isoformat(),
            "hostname": self.hostname,
            "status": status,
            "message": message,
        }
        if stats:
            metadata.update(stats)
        return self.send_heartbeat(metadata)

    def submit_metrics(self, metrics: Dict[str, Any]) -> Response:
        """Store one system-metrics sample (CPU, disk, memory) for this client.

        Collecting the numbers is the caller's business - it needs psutil, and
        what is worth collecting differs per machine. Sending them is not.
        """
        self._register_if_needed()
        result = self._post("submit_metrics",
                            {"client_id": self.client_id, "metrics": metrics})
        if result:
            self.logger.info("metrics submitted for %s", self.client_id)
        else:
            self.logger.warning("metrics submission failed for %s: %s",
                                self.client_id, result.errors)
        return result

    # -- plumbing --------------------------------------------------------

    def _register_if_needed(self) -> None:
        if self.auto_register and not self._registered:
            self._registered = True   # one attempt per process, not one per call
            self.register()

    def _post(self, action: str, payload: Dict[str, Any]) -> Response:
        """POST to the API and never raise.

        The action goes in the query string, not the body - api.php routes on
        `$_GET['action']` and ignores an action in the JSON.
        """
        try:
            response = requests.post(
                f"{self.api_url}?action={action}",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except Exception as exc:                    # noqa: BLE001
            return _failure(f"{action} request failed: {exc!r}")

        try:
            body = response.json()
        except ValueError:
            return _failure(
                f"{action} returned HTTP {response.status_code} and not JSON: "
                f"{response.text[:200]!r}"
            )

        if not isinstance(body, dict):
            return _failure(f"{action} returned unexpected JSON: {body!r}")

        # register answers 201, everything else 200; both are success, and a
        # body that says success outranks a status code that disagrees.
        if "success" not in body:
            body["success"] = response.status_code in (200, 201)
        return Response(body)


# ---------------------------------------------------------------------------
# Rotating logs
#
# Every script that vendored this client also hand-rolled its own
# `logging.basicConfig(handlers=[FileHandler(...), StreamHandler(...)])`. A
# plain FileHandler never rotates: those logs grow until checkDisk - which
# reports through this same API - complains about the partition they are on.
# It lives in this file rather than a module of its own so that the single
# vendored copy carries everything a script needs.
# ---------------------------------------------------------------------------

#: 5 MB a file, 5 old files kept: at most 30 MB per log, which is small next to
#: anything on these machines and long enough to cover a week of cron runs.
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] %(message)s"


def setup_rotating_logger(
    path: str,
    name: Optional[str] = None,
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    to_stream: bool = True,
    stream=None,
    fmt: str = DEFAULT_FORMAT,
    datefmt: Optional[str] = None,
) -> logging.Logger:
    """Configure and return a logger that writes to a size-rotated file.

    Args:
        path: Log file. Its directory is created if missing.
        name: Logger name. `None` - the default - configures the root logger,
            which is what a script replacing `logging.basicConfig` wants, and
            what makes this package's own messages land in the same file.
        level: Threshold for both handlers.
        max_bytes: Rotate once the file passes this size.
        backup_count: How many rotated files to keep.
        to_stream: Also log to a stream, so cron mail and `journalctl` still
            show the run. Every setup this replaces did this; keep it.
        stream: Which stream. `None` means stderr, the logging default; pass
            `sys.stdout` where the script it replaces used stdout.
        fmt: Format string.
        datefmt: Date format for `%(asctime)s`; `None` is the logging default.

    Calling it twice for the same logger replaces the handlers rather than
    adding a second set, so a script that is imported as well as run does not
    log everything twice.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(fmt, datefmt=datefmt)

    file_handler = RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if to_stream:
        stream_handler = logging.StreamHandler(stream)
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return logger


# ---------------------------------------------------------------------------
# Disk and mount checks
#
# The same `/` disk check ran in checkDisk.py (df), server_monitor.py (shutil),
# watchdog_daemon.py and metrics_collector.py (psutil), and two mount checks in
# rclone_monitor.py and server_monitor.py. All four disk readings are the same
# statvfs numbers; these return them once. Unlike the heartbeat, checks raise:
# a check that cannot run is a failed check, and the caller reports it.
# ---------------------------------------------------------------------------


def disk_usage(path: str = "/") -> Dict[str, Any]:
    """Disk usage of the filesystem holding `path`.

    `total`, `used`, `free` are bytes and equal `df -B1 --output=size,used,avail`
    (and psutil's). `free_percent` is free/total. `used_percent` is df's
    `Use%` and psutil's `percent` before rounding: used/(used+free). Raises
    OSError if `path` cannot be read.
    """
    usage = shutil.disk_usage(path)
    return {
        "path": path,
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "free_percent": usage.free / usage.total * 100,
        "used_percent": usage.used / (usage.used + usage.free) * 100,
    }


def mount_responds(path: str, timeout: int = 5) -> Tuple[bool, Optional[str]]:
    """Whether `ls path` answers within `timeout` seconds: (ok, error or None).

    Catches the two ways a dead FUSE mount shows itself - a hang and
    "Transport endpoint is not connected" - without hanging the caller.
    """
    try:
        result = subprocess.run(
            ["timeout", str(timeout), "ls", path],
            capture_output=True,
            text=True,
            timeout=timeout + 1,
        )
        if result.returncode == 0:
            return (True, None)
        elif "Transport endpoint is not connected" in result.stderr:
            return (False, "Mount disconnected (FUSE endpoint not connected)")
        elif result.returncode == 124:  # timeout exit code
            return (False, "Mount not responding (timeout)")
        else:
            return (False, f"Mount error: {result.stderr.strip()}")
    except subprocess.TimeoutExpired:
        return (False, "Mount not responding (timeout)")
    except Exception as e:
        return (False, f"Mount test failed: {str(e)}")


def mount_read_write(mount_point: str, test_dir: str, timeout: int = 15) -> None:
    """Check `mount_point` is mounted, then write, read back and delete a file.

    Returns None when all of that works. Raises RuntimeError naming the step
    that failed, or subprocess.TimeoutExpired if the mount hangs - callers
    tell the two apart, a hang being the usual rclone failure.

    `os.path.ismount()` rather than `mountpoint -q`: the latter is confused by
    the stacked FUSE mounts that appear when rclone restarts while the caller
    runs in a private mount namespace. Every file step runs in a subprocess
    with a timeout, because a hung mount blocks the calling thread forever.
    """
    test_file = os.path.join(test_dir, "monitor_test.txt")
    token = f"monitor-{datetime.now().isoformat()}"

    if not os.path.ismount(mount_point):
        raise RuntimeError(
            f"{mount_point} is not a mount point — rclone is not mounted"
        )

    result = subprocess.run(["mkdir", "-p", test_dir],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"mkdir failed: {result.stderr.strip()}")

    result = subprocess.run(["bash", "-c", f'echo "{token}" > "{test_file}"'],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"Write failed: {result.stderr.strip()}")

    result = subprocess.run(["cat", test_file],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"Read failed: {result.stderr.strip()}")

    content = result.stdout.strip()
    if content != token:
        raise RuntimeError(f"Read mismatch: wrote '{token}', got '{content}'")

    subprocess.run(["rm", "-f", test_file], timeout=10, capture_output=True)


# ---------------------------------------------------------------------------
# Alerts
#
# server_monitor.py posted to Discord through a 400-line discord_bot.py and
# checkDisk.py posted to Mailjet inline. Both read their credentials from the
# environment (in practice /web/zin/.env); so does this. Like the heartbeat,
# sending an alert never raises: a monitor must not die of its own alert.
# ---------------------------------------------------------------------------

DISCORD_API_BASE = "https://discord.com/api/v10"
MAILJET_SEND_URL = "https://api.mailjet.com/v3.1/send"

#: level -> (embed colour, title icon). What discord_bot.send_notification sent.
ALERT_LEVELS = {
    "info": (0x3498DB, "ℹ️"),
    "success": (0x2ECC71, "✅"),
    "warning": (0xF1C40F, "⚠️"),
    "error": (0xE74C3C, "❌"),
}


def _post_alert(url: str, ok_status: int, logger: logging.Logger, what: str,
                timeout: float, **kwargs) -> bool:
    try:
        response = requests.post(url, timeout=timeout, **kwargs)
    except Exception as exc:
        logger.error(f"Failed to send {what}: {exc}")
        return False
    if response.status_code == ok_status:
        return True
    logger.error(f"{what} error {response.status_code}: {response.text}")
    return False


def _send_discord(title, message, level, footer, env, logger, timeout) -> Optional[bool]:
    webhook_url = env.get("DISCORD_WEBHOOK_URL", "")
    bot_token = env.get("DISCORD_BOT_TOKEN", "")
    channel_id = env.get("DISCORD_CHANNEL_ID", "")
    if not webhook_url and not (bot_token and channel_id):
        return None
    color, icon = ALERT_LEVELS.get(level, (ALERT_LEVELS["info"][0], ""))
    embed: Dict[str, Any] = {"title": f"{icon} {title}", "color": color}
    if message:
        embed["description"] = message
    if footer:
        embed["footer"] = {"text": footer}
    payload = {"embeds": [embed]}
    if webhook_url:
        return _post_alert(webhook_url, 204, logger, "Discord webhook", timeout,
                           json=payload)
    return _post_alert(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages", 200, logger,
        "Discord bot API", timeout, json=payload,
        headers={"Authorization": f"Bot {bot_token}",
                 "Content-Type": "application/json"})


def _send_mailjet(title, message, env, logger, timeout, email_from, email_to,
                  from_name, to_name) -> Optional[bool]:
    api_key = env.get("MAILJET_API_KEY", "")
    secret_key = env.get("MAILJET_SECRET_KEY", "")
    email_from = email_from or env.get("ALERT_EMAIL_FROM", "")
    email_to = email_to or env.get("ALERT_EMAIL_TO", "")
    if not (api_key and secret_key and email_from and email_to):
        return None
    payload = {"Messages": [{
        "From": {"Email": email_from, "Name": from_name},
        "To": [{"Email": email_to, "Name": to_name}],
        "Subject": title,
        "TextPart": message,
    }]}
    return _post_alert(MAILJET_SEND_URL, 200, logger, "Mailjet", timeout,
                       json=payload, auth=(api_key, secret_key),
                       headers={"Content-Type": "application/json"})


def send_alert(
    title: str,
    message: str,
    level: str = "error",
    *,
    channels: Iterable[str] = ("discord", "mailjet"),
    footer: Optional[str] = None,
    email_from: Optional[str] = None,
    email_to: Optional[str] = None,
    from_name: str = "SignCollect Monitor",
    to_name: str = "Admin",
    env: Optional[Mapping[str, str]] = None,
    logger: Optional[logging.Logger] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """Send an alert to every configured channel in `channels`.

    Returns True if at least one channel accepted it. Never raises.

    - "discord": `DISCORD_WEBHOOK_URL`, or `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID`.
      An embed titled "<icon> title", coloured by `level`
      (info, success, warning, error), with `message` and `footer`.
    - "mailjet": `MAILJET_API_KEY` + `MAILJET_SECRET_KEY`; `title` is the
      subject, `message` the text. Addresses from the arguments, else
      `ALERT_EMAIL_FROM` / `ALERT_EMAIL_TO`.

    Credentials come from `env` (default: `os.environ`), never from code.
    A channel without them is skipped with a warning.
    """
    env = os.environ if env is None else env
    logger = logger or _LOG
    _ensure_visible(logger)
    sent = False
    for channel in channels:
        if channel == "discord":
            ok = _send_discord(title, message, level, footer, env, logger, timeout)
            missing = "DISCORD_WEBHOOK_URL or DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID"
        elif channel == "mailjet":
            ok = _send_mailjet(title, message, env, logger, timeout, email_from,
                               email_to, from_name, to_name)
            missing = ("MAILJET_API_KEY + MAILJET_SECRET_KEY "
                       "(and ALERT_EMAIL_FROM/ALERT_EMAIL_TO)")
        else:
            logger.error(f"Unknown alert channel {channel!r}")
            continue
        if ok is None:
            logger.warning(f"{channel} alert not sent: set {missing}")
        sent = sent or bool(ok)
    return sent
