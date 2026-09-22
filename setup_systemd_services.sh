#!/bin/bash
# Setup script to enable and start all service wrappers via systemd

echo "=========================================="
echo "Service Wrapper Systemd Setup"
echo "=========================================="
echo

# Install watchdog daemon service
echo "Installing watchdog daemon service..."
sudo cp /home/gomer/pythonCron/watchdog-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload
echo

# Enable all services
echo "Enabling all services (including watchdog)..."
sudo systemctl enable \
    watchdog-daemon.service \
    service-backup_zin_eaf_srt_files.service \
    service-check_disk.service \
    service-check_if_raw_has_post_files.service \
    service-clean_lock_files.service \
    service-convert_livelink_videos.service \
    service-convert_zinstring_to_lemmalist.service \
    service-converter.service \
    service-copy_ab_files.service \
    service-get_themas.service \
    service-match_records_for_livelink_videos_with_mocap_records.service \
    service-match_vicon_fbx_csv_files_with_mocap_records.service \
    service-move_studiofiles.service \
    service-mysql_backup.service \
    service-qrconvert.service \
    service-rclone_mount_monitor.service \
    service-sync_eaf_to_database.service \
    service-sync_mocap_files.service \
    service-sync_vicon_files_rsync.service \
    service-update_field_gvg_at_sentences.service

echo
echo "Services enabled. Starting all services..."
echo

# Start all services
sudo systemctl start \
    watchdog-daemon.service \
    service-backup_zin_eaf_srt_files.service \
    service-check_disk.service \
    service-check_if_raw_has_post_files.service \
    service-clean_lock_files.service \
    service-convert_livelink_videos.service \
    service-convert_zinstring_to_lemmalist.service \
    service-converter.service \
    service-copy_ab_files.service \
    service-get_themas.service \
    service-match_records_for_livelink_videos_with_mocap_records.service \
    service-match_vicon_fbx_csv_files_with_mocap_records.service \
    service-move_studiofiles.service \
    service-mysql_backup.service \
    service-qrconvert.service \
    service-rclone_mount_monitor.service \
    service-sync_eaf_to_database.service \
    service-sync_mocap_files.service \
    service-sync_vicon_files_rsync.service \
    service-update_field_gvg_at_sentences.service

echo
echo "=========================================="
echo "Setup complete! Checking status..."
echo "=========================================="
echo

# Show status
echo "Service wrappers:"
systemctl list-units --type=service | grep service- | grep -E 'running|failed'

echo
echo "Watchdog daemon:"
sudo systemctl status watchdog-daemon.service --no-pager | head -5

echo
echo "=========================================="
echo "Quick Reference Commands"
echo "=========================================="
echo
echo "View all services:"
echo "  systemctl list-units | grep -E 'service-|watchdog-daemon'"
echo
echo "Check specific service status:"
echo "  sudo systemctl status service-<name>.service"
echo
echo "View logs (real-time):"
echo "  sudo journalctl -u service-<name>.service -f"
echo
echo "View watchdog logs:"
echo "  sudo journalctl -u watchdog-daemon.service -f"
echo
echo "Restart a service:"
echo "  sudo systemctl restart service-<name>.service"
echo
echo "Stop/Start all services:"
echo "  sudo systemctl stop service-*.service watchdog-daemon.service"
echo "  sudo systemctl start service-*.service watchdog-daemon.service"
echo
