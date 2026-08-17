# Systemd Setup Instructions

## What I've Done

✅ **Stopped all manually-started wrapper processes** (19 processes killed)
✅ **Created systemd service file for watchdog daemon** (`watchdog-daemon.service`)
✅ **Created automated setup script** (`setup_systemd_services.sh`)

## What You Need to Do

Run the setup script with your sudo password:

```bash
bash /home/gomer/pythonCron/setup_systemd_services.sh
```

This script will:
1. Install the watchdog daemon systemd service
2. Enable all 16 service wrappers + watchdog to start on boot
3. Start all services immediately
4. Show you the status of all running services

## What the Setup Does

The script will configure systemd to manage:

### Service Wrappers (16 total)
- service-backup_zin_eaf_srt_files.service
- service-check_disk.service
- service-check_if_raw_has_post_files.service
- service-clean_lock_files.service
- service-convert_livelink_videos.service
- service-convert_zinstring_to_lemmalist.service
- service-converter.service
- service-copy_ab_files.service
- service-get_themas.service
- service-match_records_for_livelink_videos_with_mocap_records.service
- service-match_vicon_fbx_csv_files_with_mocap_records.service
- service-move_studiofiles.service
- service-mysql_backup.service
- service-qrconvert.service
- service-update_field_glosses_at_sentences.service
- service-update_field_gvg_at_sentences.service

### Watchdog Daemon
- watchdog-daemon.service (monitors all wrappers, auto-restarts crashed services)

## Benefits of Systemd Management

1. **Auto-start on boot** - All services start automatically when server reboots
2. **Auto-restart on crash** - Systemd restarts failed services
3. **Centralized logging** - All logs available via `journalctl`
4. **Easy management** - Standard systemd commands
5. **Process supervision** - Systemd monitors all processes
6. **Resource limits** - Can configure CPU/memory limits per service

## After Setup - Verify Everything Works

### Check all services are running:
```bash
systemctl list-units | grep -E 'service-|watchdog-daemon' | grep running
```

### Check watchdog status:
```bash
sudo systemctl status watchdog-daemon.service
```

### View real-time logs for a service:
```bash
sudo journalctl -u service-check_disk.service -f
```

### View watchdog logs:
```bash
sudo journalctl -u watchdog-daemon.service -f
```

## Common Operations

### Restart a service:
```bash
sudo systemctl restart service-mysql_backup.service
```

### Stop a service:
```bash
sudo systemctl stop service-converter.service
```

### Start a service:
```bash
sudo systemctl start service-converter.service
```

### View service status:
```bash
sudo systemctl status service-qrconvert.service
```

### Disable a service (prevent auto-start):
```bash
sudo systemctl disable service-get_themas.service
```

### Re-enable a service:
```bash
sudo systemctl enable service-get_themas.service
```

## Troubleshooting

### If a service won't start:
```bash
# Check logs
sudo journalctl -u service-<name>.service -n 50

# Check service file
cat /etc/systemd/system/service-<name>.service

# Check configuration
cat /home/gomer/pythonCron/services_config.json | grep -A 20 "<service_name>"
```

### If watchdog isn't working:
```bash
# Check watchdog status
sudo systemctl status watchdog-daemon.service

# View watchdog logs
sudo journalctl -u watchdog-daemon.service -n 100

# Restart watchdog
sudo systemctl restart watchdog-daemon.service
```

### View all service logs together:
```bash
sudo journalctl -u 'service-*' -f
```

## Known Issues to Fix

1. **Get_Themas service failing** - PHP error: Class "SecurityHeaders" not found
   - This is an application issue, not infrastructure
   - Fix the PHP script or disable the service temporarily

## Files Created

- `/home/gomer/pythonCron/setup_systemd_services.sh` - Setup automation script
- `/home/gomer/pythonCron/watchdog-daemon.service` - Watchdog systemd unit file
- `/home/gomer/pythonCron/SYSTEMD_SETUP_INSTRUCTIONS.md` - This file

## Next Steps After Setup

1. Run the setup script
2. Verify all services are running
3. Monitor logs for a few hours
4. Fix the Get_Themas PHP error
5. Set up log rotation if needed (journalctl handles this automatically)

## Comparison: Before vs After

| Aspect | Before (Manual) | After (Systemd) |
|--------|----------------|-----------------|
| Start method | `nohup python3 ...` | `systemctl start` |
| Auto-start on boot | No | Yes |
| Auto-restart on crash | Only via watchdog | Systemd + watchdog |
| Logs | Separate files | journalctl (centralized) |
| Process management | Manual `pkill` | systemctl commands |
| Status checking | `ps aux \| grep` | systemctl status |
| Resource limits | None | Configurable |
