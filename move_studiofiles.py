"""
Move studio files into the correct date folders, mirror converted/thumbnail
files into studioFilesMini, and record per-day file counts.

All directory listing is done directly against the rclone remote with a single
recursive `rclone lsf`, and file moves are server-side `rclone moveto` calls on
the remote. Nothing walks the FUSE mount any more: the previous version listed
~500k entries through the mount every run, which kept the mount's directory
cache (and its garbage collector) permanently busy, and moved files by
downloading and re-uploading them through FUSE, which timed out on every run.

Usage:
    python3 move_studiofiles.py            # plan and execute
    python3 move_studiofiles.py --dry-run  # only print what would be done
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Callable, Dict, Iterable, List, Tuple

from sc_paths import sc_path

# Log setup: the installed package, else the copy vendored beside this script
# (see README.md), else the stdlib.
try:
    from signlab_client_monitor import setup_rotating_logger
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from python_client import setup_rotating_logger
    except ImportError:
        # Last resort, stdlib only: a missing file or dependency must never
        # stop this process from starting. Same files and rotation.
        import logging.handlers

        def setup_rotating_logger(path, name=None, level=logging.INFO,
                                  max_bytes=5 * 1024 * 1024, backup_count=5,
                                  to_stream=True, stream=None,
                                  fmt="[%(asctime)s] [%(levelname)s] %(message)s",
                                  datefmt=None):
            logger = logging.getLogger(name)
            logger.setLevel(level)
            handlers = [logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backup_count)]
            if to_stream:
                handlers.append(logging.StreamHandler(stream))
            for handler in handlers:
                handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
                logger.addHandler(handler)
            return logger

# Runtime data goes beside the script (production: /home/gomer/pythonCron),
# or under $PYTHONCRON_STATE_DIR, as scheduler_v2.py does.
STATE_DIR = os.environ.get('PYTHONCRON_STATE_DIR') or os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(STATE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

# Root logger: logs/move_videos.log (5 MB x 5) and stderr.
setup_rotating_logger(os.path.join(LOG_DIR, 'move_videos.log'),
                      fmt='%(asctime)s - %(levelname)s - %(message)s')

RCLONE_BIN = os.environ.get("RCLONE_BIN", "rclone")
REMOTE = "signcollect:/AIHR-FGW-TEST-SIGNLAB (Projectfolder)/studioFiles"
RCLONE_LIST_TIMEOUT_SECONDS = 3600
RCLONE_MOVE_TIMEOUT_SECONDS = 600   # server-side rename, normally takes seconds
RCLONE_COPY_TIMEOUT_SECONDS = 1800  # download of one converted video

MINI_RAW_DIR = sc_path("media_raw")
STUDIO_DATA_JSON = sc_path("studio_data.json")
RAW_SUBDIR = "raw"
MIRROR_SUBDIRS = ("converted", "thumbnails")
COUNT_PREFIXES = "LMRAB"  # order of the columns in studio_data

DATE_DIR_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILENAME_DATE_REGEX = re.compile(r"^[ABLMR](\d{8})_\d+\.MP4$", re.IGNORECASE)


@dataclass
class Plan:
    moves: List[Tuple[str, str]] = field(default_factory=list)    # (remote src, remote dst), relative to REMOTE
    copies: List[Tuple[str, str]] = field(default_factory=list)   # (remote src relative, local absolute dest)
    counts: Dict[str, List[int]] = field(default_factory=dict)    # date folder -> [L, M, R, A, B]
    warnings: List[str] = field(default_factory=list)


def _target_for_mismatch(filename: str) -> str:
    """Return 'YYYY-MM-DD' derived from the filename, or '' if the name has no valid date."""
    match = FILENAME_DATE_REGEX.search(filename)
    if not match:
        return ""
    d = match.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def _count(files: Iterable[str], compact_date: str) -> List[int]:
    counts = [0] * len(COUNT_PREFIXES)
    for name in files:
        if not name.lower().endswith(".mp4") or compact_date not in name:
            continue
        idx = COUNT_PREFIXES.find(name[0]) if name else -1
        if idx >= 0:
            counts[idx] += 1
    return counts


def build_plan(listing: Iterable[str], local_exists: Callable[[str], bool]) -> Plan:
    """
    Turn the output of `rclone lsf -R` (relative paths, directories end in '/')
    into a plan of moves, copies and counts. Pure: performs no I/O.
    """
    dirs = set()
    files_by_dir: Dict[str, List[str]] = {}
    for line in listing:
        line = line.rstrip("\n")
        if not line:
            continue
        if line.endswith("/"):
            dirs.add(line[:-1])
        else:
            p = PurePosixPath(line)
            files_by_dir.setdefault(str(p.parent), []).append(p.name)

    plan = Plan()
    date_folders = sorted(
        (d for d in dirs if "/" not in d and DATE_DIR_REGEX.match(d)), reverse=True
    )
    # raw contents per date folder after the planned moves, used for counting
    raw_after_moves: Dict[str, List[str]] = {}

    for folder in date_folders:
        if f"{folder}/{RAW_SUBDIR}" not in dirs:
            plan.warnings.append(f"No raw directory in folder: {folder}. Skipping.")
            continue
        raw_after_moves.setdefault(folder, [])

    for folder in list(raw_after_moves):
        compact_date = folder.replace("-", "")
        raw_dir = f"{folder}/{RAW_SUBDIR}"
        for name in files_by_dir.get(raw_dir, []):
            if compact_date in name:
                raw_after_moves[folder].append(name)
                continue
            target_folder = _target_for_mismatch(name)
            if not target_folder:
                plan.warnings.append(
                    f"Filename does not contain a valid date: {raw_dir}/{name}. Skipping.")
                continue
            plan.moves.append((f"{raw_dir}/{name}", f"{target_folder}/{RAW_SUBDIR}/{name}"))
            if target_folder in raw_after_moves:
                raw_after_moves[target_folder].append(name)

        for sub in MIRROR_SUBDIRS:
            sub_dir = f"{folder}/{sub}"
            if sub_dir not in dirs:
                continue
            for name in files_by_dir.get(sub_dir, []):
                stem, ext = os.path.splitext(name)
                dest = f"{MINI_RAW_DIR}/{stem}{ext.lower()}"
                if not local_exists(dest):
                    plan.copies.append((f"{sub_dir}/{name}", dest))

    for folder, names in raw_after_moves.items():
        plan.counts[folder] = _count(names, folder.replace("-", ""))

    return plan


# --------------------------------------------------------------------------- I/O

RCLONE_DIR_NOT_FOUND_EXIT = 3


def _lsf(path: str) -> List[str]:
    """Non-recursive listing of REMOTE/path. Returns [] if the directory does not exist."""
    result = subprocess.run(
        [RCLONE_BIN, "lsf", f"{REMOTE}/{path}" if path else REMOTE,
         # A single PROPFIND to Nextcloud occasionally stalls; rclone's default idle
         # timeout is 5 minutes, which turned a 1-minute listing into 13 minutes.
         "--timeout", "30s", "--contimeout", "15s", "--retries", "3", "--low-level-retries", "3"],
        capture_output=True, text=True, timeout=RCLONE_LIST_TIMEOUT_SECONDS,
    )
    if result.returncode == RCLONE_DIR_NOT_FOUND_EXIT:
        return []
    if result.returncode != 0:
        raise RuntimeError(f"rclone lsf {path!r} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.splitlines()


def list_remote() -> List[str]:
    """
    Build an `rclone lsf -R`-style listing (relative paths, dirs end in '/') of only
    the parts of the tree the plan needs: each date folder plus its raw/converted/
    thumbnails subfolders. Date folders also hold large post/ and AB/ trees that a full
    recursive listing would spend most of its time on.
    """
    logging.info(f"Listing date folders in {REMOTE}")
    top = _lsf("")
    date_folders = [d[:-1] for d in top if d.endswith("/") and DATE_DIR_REGEX.match(d[:-1])]
    lines: List[str] = [f"{d}/" for d in date_folders]

    def list_sub(folder: str, sub: str) -> List[str]:
        entries = _lsf(f"{folder}/{sub}")
        # _lsf returns [] both for a missing and an empty directory; probe the parent
        # so an empty raw/ is still recorded as present (plan skips folders without raw).
        if not entries and f"{sub}/" not in _lsf(folder):
            return []
        return [f"{folder}/{sub}/"] + [f"{folder}/{sub}/{e}" for e in entries]

    jobs = [(d, sub) for d in date_folders for sub in (RAW_SUBDIR, *MIRROR_SUBDIRS)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        for chunk in executor.map(lambda j: list_sub(*j), jobs):
            lines.extend(chunk)
    logging.info(f"Listing done: {len(date_folders)} date folders, {len(lines)} entries")
    return lines


def rclone_moveto(src_rel: str, dst_rel: str) -> None:
    cmd = [RCLONE_BIN, "moveto", f"{REMOTE}/{src_rel}", f"{REMOTE}/{dst_rel}", "--log-level=NOTICE"]
    subprocess.run(cmd, check=True, timeout=RCLONE_MOVE_TIMEOUT_SECONDS)
    logging.info(f"Rclone moved: {src_rel} -> {dst_rel}")


def rclone_copyto(src_rel: str, dest: str) -> None:
    cmd = [RCLONE_BIN, "copyto", f"{REMOTE}/{src_rel}", dest, "--log-level=NOTICE"]
    subprocess.run(cmd, check=True, timeout=RCLONE_COPY_TIMEOUT_SECONDS)
    logging.info(f"Copied and renamed: {src_rel} -> {dest}")


def execute_plan(plan: Plan) -> None:
    for src, dst in plan.moves:
        try:
            rclone_moveto(src, dst)
        except Exception as e:
            logging.error(f"Rclone failed to move {src} to {dst}: {e}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(rclone_copyto, src, dest): src for src, dest in plan.copies}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logging.error(f"Failed to copy {futures[future]}: {e}")


# Credentials come from the environment or <root>/zin/.env (normally
# /web/zin/.env), the same DB_* keys server_monitor.py reads. Never a literal.
def _load_zin_env() -> None:
    env_file = sc_path("zin", ".env")
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"\''))
    except OSError:
        pass


def _db_config() -> dict:
    _load_zin_env()
    return {
        'host': os.getenv('DB_HOST', 'localhost'),
        'user': os.getenv('DB_USER', 'user'),
        'password': os.getenv('DB_PASSWORD', ''),
        'database': os.getenv('DB_NAME', 'admin_gebarenoverleg'),
    }


def get_db_connection():
    import mysql.connector  # only needed when writing counts; keeps tests import-light
    try:
        connection = mysql.connector.connect(**_db_config())
        if connection.is_connected():
            return connection
    except mysql.connector.Error as err:
        logging.error(f"Error connecting to MySQL: {err}")
    return None


def save_counts(counts: Dict[str, List[int]]) -> None:
    """Update <root>/studio_data.json and the studio_data table with the L/M/R/A/B counts."""
    with open(STUDIO_DATA_JSON, 'r') as f:
        data = json.load(f)
    data.update(counts)
    data = dict(sorted(data.items(), reverse=True))
    with open(STUDIO_DATA_JSON, 'w') as f:
        json.dump(data, f)

    conn = get_db_connection()
    if not conn:
        logging.error("Failed to connect to the database; counts not written to studio_data.")
        return
    try:
        cursor = conn.cursor()
        sql = ("UPDATE studio_data SET L_count = %s, M_count = %s, R_count = %s, "
               "A_count = %s, B_count = %s WHERE date = %s")
        for date, (l, m, r, a, b) in counts.items():
            cursor.execute(sql, (l, m, r, a, b, date))
        conn.commit()
    finally:
        conn.close()


def main(dry_run: bool = False) -> Plan:
    plan = build_plan(list_remote(), local_exists=os.path.exists)

    for w in plan.warnings:
        logging.warning(w)
    logging.info(f"Plan: {len(plan.moves)} moves, {len(plan.copies)} copies, "
                 f"{len(plan.counts)} date folders to count")
    if dry_run:
        for src, dst in plan.moves:
            print(f"MOVE  {src} -> {dst}")
        for src, dest in plan.copies:
            print(f"COPY  {src} -> {dest}")
        for folder in sorted(plan.counts, reverse=True)[:10]:
            print(f"COUNT {folder} {plan.counts[folder]}")
        return plan

    execute_plan(plan)
    save_counts(plan.counts)
    logging.info("All moves and copies completed.")
    return plan


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="plan only, change nothing")
    args = parser.parse_args()

    if args.dry_run:
        main(dry_run=True)
        sys.exit(0)

    try:
        from signlab_client_monitor import ClientMonitor
    except ImportError:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from python_client import ClientMonitor
    monitor = ClientMonitor(
        api_url="https://signcollect.nl/client_monitor_api/api.php",
        client_id="move-studio-files",
        client_name="Move Studio Files",
        description="Moves studio files to correct date folders and counts them",
        heartbeat_interval=21600  # 6 hours
    )
    try:
        plan = main()
        monitor.send_heartbeat_with_stats(
            status="success",
            message="Studio file processing completed",
            stats={
                "timestamp": datetime.now().isoformat(),
                "moves": len(plan.moves),
                "copies": len(plan.copies),
            }
        )
    except Exception as e:
        logging.error(f"Studio file processing failed: {e}")
        monitor.send_heartbeat_with_stats(
            status="error",
            message=f"Studio file processing failed: {str(e)}",
            stats={"error_type": type(e).__name__}
        )
        raise
