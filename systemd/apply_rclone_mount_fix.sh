#!/bin/bash
# Applies the rclone mount fix prepared on 2026-09-11 (needs root).
# 1. installs the updated unit (no GOMEMLIMIT, /usr/local/bin/rclone 1.75.1, --rc)
# 2. stops the Move_StudioFiles wrapper so nothing is mid-transfer through the mount
# 3. restarts the mount (server-monitor.service Requires= it and comes back with it)
# 4. starts the wrapper again
set -euo pipefail
U=/etc/systemd/system/rclone-mount.service
cp -n "$U" "$U.bak.20260911" || true
install -m 0644 /home/gomer/pythonCron/systemd/rclone-mount.service "$U"
systemctl daemon-reload
systemd-analyze verify rclone-mount.service

systemctl stop service-move_studiofiles.service
systemctl restart rclone-mount.service
sleep 5
systemctl --no-pager --lines=5 status rclone-mount.service server-monitor.service | grep -E 'Active|rclone\['
mountpoint /web/gebarenoverleg_media/studioFiles
timeout 30 ls /web/gebarenoverleg_media/studioFiles | wc -l
systemctl start service-move_studiofiles.service

echo; echo "new mount process:"; ps -o pid,pcpu,rss,args -C rclone | grep mount | cut -c1-120
echo; echo "GC/heap check (rc):"; curl -s -X POST http://127.0.0.1:5572/core/memstats | head -c 400; echo
