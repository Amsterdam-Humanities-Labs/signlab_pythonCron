import os
import shutil
import subprocess
import sys
import glob
from datetime import datetime

sys.path.insert(0, '/home/gomer/pythonCron')
from python_client import ClientMonitor

# Initialize Client Monitor
monitor = ClientMonitor(
    api_url="https://signcollect.nl/client_monitor_api/api.php",
    client_id="backup-zin-eaf-srt",
    client_name="Zin Backup",
    description="Backs up .eaf and .srt files from zin directory as zip archive",
    heartbeat_interval=86400  # 1440 minutes (24 hours)
)

SOURCE_DIR = "/web/zin/eaf/zin/"
REMOTE_DIR = "/web/gebarenoverleg_media/studioFiles/zinBackup/"
LOCAL_TMP = "/tmp"
KEEP_BACKUPS = 3


def backup_zin_files():
    today = datetime.now().strftime("%Y-%m-%d")
    zip_name = f"zinBackup_{today}"
    local_zip_path = os.path.join(LOCAL_TMP, zip_name)  # without .zip, shutil adds it

    # Step 1: Create zip locally (fast, local disk)
    print(f"Zipping .eaf and .srt files from {SOURCE_DIR} ...")
    file_count = 0
    import zipfile
    full_zip_path = local_zip_path + ".zip"
    with zipfile.ZipFile(full_zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for entry in os.scandir(SOURCE_DIR):
            if entry.is_file() and (entry.name.endswith('.eaf') or entry.name.endswith('.srt')):
                zf.write(entry.path, entry.name)
                file_count += 1

    zip_size_mb = round(os.path.getsize(full_zip_path) / (1024 * 1024), 2)
    print(f"Zip created: {full_zip_path} ({zip_size_mb} MB, {file_count} files)")

    # Step 2: Copy zip to rclone mount
    os.makedirs(REMOTE_DIR, exist_ok=True)
    remote_zip_path = os.path.join(REMOTE_DIR, zip_name + ".zip")
    print(f"Copying to {remote_zip_path} ...")
    shutil.copy2(full_zip_path, remote_zip_path)
    print("Copy complete.")

    # Step 3: Remove local tmp zip
    os.remove(full_zip_path)

    # Step 4: Clean up old backups, keep last N
    existing = sorted(glob.glob(os.path.join(REMOTE_DIR, "zinBackup_*.zip")))
    removed = 0
    if len(existing) > KEEP_BACKUPS:
        for old in existing[:-KEEP_BACKUPS]:
            print(f"Removing old backup: {os.path.basename(old)}")
            os.remove(old)
            removed += 1

    stats = {
        "file_count": file_count,
        "zip_size_mb": zip_size_mb,
        "backups_kept": min(len(existing), KEEP_BACKUPS),
        "old_removed": removed,
    }

    print(f"\nBackup Summary:")
    print(f"  Files archived: {file_count}")
    print(f"  Zip size: {zip_size_mb} MB")
    print(f"  Old backups removed: {removed}")
    print(f"  Backups on remote: {stats['backups_kept']}")

    return stats


if __name__ == "__main__":
    try:
        stats = backup_zin_files()

        monitor.send_heartbeat_with_stats(
            status="success",
            message=f"Zin backup completed. {stats['file_count']} files, {stats['zip_size_mb']} MB zip",
            stats=stats
        )

    except Exception as e:
        monitor.send_heartbeat_with_stats(
            status="error",
            message=f"Zin backup failed: {str(e)}",
            stats={"error_type": type(e).__name__}
        )
        raise
