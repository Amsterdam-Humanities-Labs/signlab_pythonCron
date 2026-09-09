#!/usr/bin/env python3
"""
Sync mocap-related files from local source directories to the rclone remote.
Uses rclone copy directly (bypasses FUSE mount) for reliable, fast transfers.
For dirs marked delete_after_sync, deletes local files once confirmed on remote.
Designed to handle hundreds of thousands of files.
"""

import os
import subprocess
import sys
import re
from datetime import datetime

# The heartbeat client. Prefer the installed package; fall back to the copy
# vendored in this directory, which is what a host that has never run
# client/install.sh from signlab_client_monitor_api will find. Note the
# fallback finds it *beside this script* rather than at a path hardcoded to
# one particular server's home directory.
try:
    from signlab_client_monitor import ClientMonitor
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from python_client import ClientMonitor

monitor = ClientMonitor(
    api_url="https://signcollect.nl/client_monitor_api/api.php",
    client_id="sync-mocap-files",
    client_name="Sync Mocap Files",
    description="Syncs mocap files (fbx, csv, json, mov, mkv) to studioFiles/mocapFiles on rclone mount",
    heartbeat_interval=21600  # 6 hours
)

RCLONE_REMOTE = "signcollect:/AIHR-FGW-TEST-SIGNLAB (Projectfolder)/studioFiles/mocapFiles"

# Per-file timeout for rclone (5 min idle before giving up on a file)
RCLONE_TIMEOUT = "5m"
# Max concurrent transfers
RCLONE_TRANSFERS = 4

# (source, dest_subfolder, includes, delete_after_sync)
SYNC_MAP = [
    ("/web/gebarenoverleg_media/fbx",         "unreal",       ["*.fbx", "*.glb"], False),
    ("/web/gebarenoverleg_media/fbx/CC",      "unreal/CC",    ["*.fbx", "*.glb"], False),
    ("/web/gebarenoverleg_media/fbx/Vicon",   "unreal/Vicon", ["*.fbx", "*.glb"], False),
    ("/web/gebarenoverleg_media/llcsv",        "livelink",     ["*.csv"],          False),
    ("/web/gebarenoverleg_media/metadata",     "metadata",     ["*.json"],         False),
    ("/web/gebarenoverleg_media/shogun_live",  "shogun_live",  ["*.mov", "*.mcp", "*.enf", "*.x2d"], True),
    ("/web/gebarenoverleg_media/razerFiles",   "obs",          ["*.mkv"],          False),
]


def sync_folder(source, dest_subfolder, includes):
    """Use rclone copy to sync files from source to remote dest."""
    if not os.path.isdir(source):
        return {"skipped": True, "reason": f"Source not found: {source}"}

    dest = f"{RCLONE_REMOTE}/{dest_subfolder}"

    if not source.endswith('/'):
        source += '/'

    cmd = [
        'rclone', 'copy', source, dest,
        '--timeout', RCLONE_TIMEOUT,
        '--retries', '3',
        '--low-level-retries', '10',
        '--transfers', str(RCLONE_TRANSFERS),
        '--stats-one-line',
        '--stats', '30s',
        '-v',
    ]
    for pattern in includes:
        cmd += ['--include', pattern]

    result = subprocess.run(cmd, capture_output=True, text=True)

    output = result.stderr
    transferred = 0
    errors = 0
    checks = 0

    for line in output.splitlines():
        m = re.search(r'Transferred:\s+(\d+) / (\d+)', line)
        if m:
            transferred = int(m.group(1))
        m = re.search(r'Checks:\s+(\d+)', line)
        if m:
            checks = int(m.group(1))
        m = re.search(r'Errors:\s+(\d+)', line)
        if m:
            errors = int(m.group(1))

    copied = len([l for l in output.splitlines() if ': Copied' in l])
    if copied > transferred:
        transferred = copied

    error_msgs = [l for l in output.splitlines() if 'ERROR' in l]

    if result.returncode != 0 and transferred == 0:
        return {
            "transferred": 0,
            "errors": errors or 1,
            "checks": checks,
            "error_msgs": error_msgs[:10],
        }

    return {
        "transferred": transferred,
        "errors": errors,
        "checks": checks,
        "error_msgs": error_msgs[:10],
    }


def cleanup_synced_files(source, dest_subfolder, includes):
    """Delete local files that are confirmed to exist on remote with same name and size."""
    dest = f"{RCLONE_REMOTE}/{dest_subfolder}"

    if not os.path.isdir(source):
        return {"deleted": 0, "skipped": 0, "freed_bytes": 0}

    # Get remote file list with sizes via rclone lsjson
    cmd = ['rclone', 'lsjson', dest, '--no-modtime', '--no-mimetype']
    for pattern in includes:
        cmd += ['--include', pattern]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"    Failed to list remote files: {result.stderr.strip()}", flush=True)
        return {"deleted": 0, "skipped": 0, "freed_bytes": 0, "error": result.stderr.strip()}

    import json
    try:
        remote_files = {f['Name']: f['Size'] for f in json.loads(result.stdout) if not f.get('IsDir')}
    except (json.JSONDecodeError, KeyError) as e:
        print(f"    Failed to parse remote file list: {e}", flush=True)
        return {"deleted": 0, "skipped": 0, "freed_bytes": 0, "error": str(e)}

    # Compare local vs remote and delete confirmed matches
    exts = [p.replace('*', '') for p in includes]
    deleted = 0
    skipped = 0
    freed_bytes = 0

    for entry in os.scandir(source):
        if not entry.is_file(follow_symlinks=False):
            continue
        if not any(entry.name.lower().endswith(ext) for ext in exts):
            continue
        try:
            local_size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue

        remote_size = remote_files.get(entry.name)
        if remote_size is not None and remote_size == local_size:
            try:
                os.remove(entry.path)
                deleted += 1
                freed_bytes += local_size
            except OSError as e:
                print(f"    Failed to delete {entry.name}: {e}", flush=True)
                skipped += 1
        else:
            skipped += 1

    return {"deleted": deleted, "skipped": skipped, "freed_bytes": freed_bytes}


def main():
    results = {}
    total_transferred = 0
    total_errors = 0
    total_deleted = 0
    total_freed = 0
    all_error_msgs = []

    for source, dest_subfolder, includes, delete_after_sync in SYNC_MAP:
        label = dest_subfolder
        print(f"Syncing {source} -> {dest_subfolder} ...", flush=True)
        try:
            result = sync_folder(source, dest_subfolder, includes)
            if result.get("skipped"):
                print(f"  Skipped: {result['reason']}", flush=True)
                results[label] = "skipped"
            else:
                count = result["transferred"]
                errs = result.get("errors", 0)
                checks = result.get("checks", 0)
                total_transferred += count
                total_errors += errs
                print(f"  Checked: {checks}, Transferred: {count}, Errors: {errs}", flush=True)
                results[label] = count
                if result.get("error_msgs"):
                    for msg in result["error_msgs"]:
                        print(f"    {msg}", flush=True)
                    all_error_msgs.extend(
                        f"{label}: {m}" for m in result["error_msgs"]
                    )

                # Cleanup local files after successful sync (no rclone errors)
                if delete_after_sync and errs == 0:
                    print(f"  Cleaning up local files ...", flush=True)
                    cleanup = cleanup_synced_files(source, dest_subfolder, includes)
                    d = cleanup["deleted"]
                    s = cleanup["skipped"]
                    freed = cleanup["freed_bytes"]
                    total_deleted += d
                    total_freed += freed
                    print(f"  Cleanup: {d} deleted, {s} skipped, {freed / (1024**3):.2f} GB freed", flush=True)
                    if cleanup.get("error"):
                        all_error_msgs.append(f"{label} cleanup: {cleanup['error']}")

        except Exception as e:
            print(f"  ERROR: {e}", flush=True)
            total_errors += 1
            all_error_msgs.append(f"{label}: {e}")
            results[label] = f"error: {e}"

    print(f"\nSync Summary:", flush=True)
    print(f"  Total files transferred: {total_transferred}", flush=True)
    print(f"  Total files deleted locally: {total_deleted}", flush=True)
    print(f"  Total space freed: {total_freed / (1024**3):.2f} GB", flush=True)
    print(f"  Total errors: {total_errors}", flush=True)
    for label, count in results.items():
        print(f"  {label}: {count}", flush=True)

    return {
        "total_transferred": total_transferred,
        "total_deleted": total_deleted,
        "total_freed_gb": round(total_freed / (1024**3), 2),
        "details": results,
        "error_count": total_errors,
        "errors": all_error_msgs,
    }


if __name__ == "__main__":
    try:
        stats = main()

        if stats["error_count"] > 0:
            status = "warning"
            message = f"Mocap sync completed with {stats['error_count']} errors. {stats['total_transferred']} transferred, {stats['total_deleted']} cleaned up, {stats['total_freed_gb']} GB freed."
        else:
            status = "success"
            message = f"Mocap sync completed. {stats['total_transferred']} transferred, {stats['total_deleted']} cleaned up, {stats['total_freed_gb']} GB freed."

        monitor.send_heartbeat_with_stats(status=status, message=message, stats=stats)

    except Exception as e:
        monitor.send_heartbeat_with_stats(
            status="error",
            message=f"Mocap sync failed: {str(e)}",
            stats={"error_type": type(e).__name__}
        )
        raise
