#!/usr/bin/env python3
"""
Rclone Mount Monitor

This script monitors the rclone mount process to ensure it's running correctly.
It checks:
- If rclone process is running
- If it's the correct rclone mount (signcollect)
- Process stats (PID, CPU%, MEM%, uptime)

Sends heartbeat to Client Monitor API with status and stats.
"""

import subprocess
import sys
import os
import re
from datetime import datetime

# The heartbeat client. Prefer the installed package; fall back to the copy
# vendored in this directory, which is what a host that has never run
# client/install.sh from signlab_client_monitor_api will find. Note the
# fallback finds it *beside this script* rather than at a path hardcoded to
# one particular server's home directory.
try:
    from signlab_client_monitor import ClientMonitor, mount_responds
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from python_client import ClientMonitor, mount_responds

# Initialize Client Monitor
monitor = ClientMonitor(
    api_url="https://signcollect.nl/client_monitor_api/api.php",
    client_id="rclone-mount-monitor",
    client_name="Rclone Mount Monitor",
    description="Monitors rclone mount process for signcollect remote",
    heartbeat_interval=3600  # 60 minutes
)


def get_rclone_process():
    """
    Check if rclone process is running and get its details.

    Returns:
        dict: Process details if running, None if not running
    """
    try:
        # Get all rclone processes
        result = subprocess.run(
            ['ps', 'aux'],
            capture_output=True,
            text=True,
            check=True
        )

        # Look for rclone mount processes
        for line in result.stdout.split('\n'):
            if 'rclone' in line and 'mount' in line and 'signcollect' in line:
                # Parse ps aux output
                # USER       PID %CPU %MEM    VSZ   RSS TTY      STAT START   TIME COMMAND
                parts = line.split(None, 10)
                if len(parts) >= 11:
                    return {
                        'user': parts[0],
                        'pid': parts[1],
                        'cpu_percent': parts[2],
                        'mem_percent': parts[3],
                        'vsz': parts[4],
                        'rss': parts[5],
                        'tty': parts[6],
                        'stat': parts[7],
                        'start_time': parts[8],
                        'cpu_time': parts[9],
                        'command': parts[10]
                    }

        return None

    except subprocess.CalledProcessError as e:
        print(f"Error running ps command: {e}")
        return None
    except Exception as e:
        print(f"Error parsing process list: {e}")
        return None


def extract_mount_path(command):
    """
    Extract the mount path from the rclone command.

    Args:
        command (str): The full rclone command string

    Returns:
        str: The mount path or "unknown"
    """
    # Look for the mount path - it's the first argument starting with /
    # after the 'mount' keyword
    parts = command.split()

    for i, part in enumerate(parts):
        if part == 'mount':
            # Look for the first path starting with / after 'mount'
            for j in range(i + 1, len(parts)):
                if parts[j].startswith('/') and not parts[j].startswith('/-'):
                    return parts[j]

    return "unknown"


# `ls` the mount with a 5 s timeout: (is_accessible, error message or None).
test_mount_accessible = mount_responds


def main():
    """Main function to check rclone status and send heartbeat"""

    try:
        # Get rclone process details
        process = get_rclone_process()

        if process:
            # Rclone process is running
            mount_path = extract_mount_path(process['command'])

            # Verify it's the correct mount (should contain gebarenoverleg_media)
            is_correct_mount = 'gebarenoverleg_media' in mount_path or 'web' in mount_path

            # Test if mount is actually accessible
            is_accessible, mount_error = test_mount_accessible(mount_path)

            # Determine status based on process and mount accessibility
            if not is_accessible:
                status = "error"
                message = f"Rclone process running (PID: {process['pid']}) but mount BROKEN: {mount_error}"
            elif not is_correct_mount:
                status = "warning"
                message = f"Rclone process running (PID: {process['pid']}) - WARNING: Unexpected mount path: {mount_path}"
            else:
                status = "success"
                message = f"Rclone mount healthy (PID: {process['pid']})"

            # Send heartbeat with process stats
            monitor.send_heartbeat_with_stats(
                status=status,
                message=message,
                stats={
                    "status": "running" if is_accessible else "broken",
                    "pid": process['pid'],
                    "cpu_percent": float(process['cpu_percent']),
                    "mem_percent": float(process['mem_percent']),
                    "mount_path": mount_path,
                    "start_time": process['start_time'],
                    "cpu_time": process['cpu_time'],
                    "is_correct_mount": is_correct_mount,
                    "is_accessible": is_accessible,
                    "mount_error": mount_error
                }
            )

            print(f"Rclone monitor: Process running (PID: {process['pid']})")
            print(f"  CPU: {process['cpu_percent']}%")
            print(f"  MEM: {process['mem_percent']}%")
            print(f"  Mount: {mount_path}")
            print(f"  Accessible: {'YES' if is_accessible else 'NO - ' + mount_error}")

        else:
            # Rclone is not running
            monitor.send_heartbeat_with_stats(
                status="error",
                message="Rclone process NOT running",
                stats={
                    "status": "stopped",
                    "pid": None,
                    "cpu_percent": 0,
                    "mem_percent": 0,
                    "mount_path": None
                }
            )

            print("Rclone monitor: Process NOT running!")

    except Exception as e:
        # Send error heartbeat
        monitor.send_heartbeat_with_stats(
            status="error",
            message=f"Rclone monitor failed: {str(e)}",
            stats={"error_type": type(e).__name__}
        )
        raise  # Re-raise to maintain existing error behavior


if __name__ == "__main__":
    main()
