#with this script we can check the disk space of the system
#writes disk usage to diskCheck.json and mails the admin when free space
#drops below ALERT_FREE_PERCENT

import json
import os
import sys
from sc_paths import sc_path
# The heartbeat client. Prefer the installed package; fall back to the copy
# vendored in this directory, which is what a host that has never run
# client/install.sh from signlab_client_monitor_api will find. Note the
# fallback finds it *beside this script* rather than at a path hardcoded to
# one particular server's home directory.
try:
    from signlab_client_monitor import ClientMonitor, disk_usage, send_alert
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from python_client import ClientMonitor, disk_usage, send_alert

# Mail alert when free space on / is below this percentage. server_monitor.py
# sends its own Discord alert at 10%; this mail is the earlier warning.
ALERT_FREE_PERCENT = 30

# Load credentials from <root>/zin/.env, normally /web/zin/.env (see README.md)
ENV_FILE = sc_path("zin", ".env")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

# Initialize Client Monitor
monitor = ClientMonitor(
    api_url="https://signcollect.nl/client_monitor_api/api.php",
    client_id="check-disk",
    client_name="Disk Space Monitor",
    description="Monitors disk space and sends alerts when space is low",
    heartbeat_interval=3600  # 60 minutes
)

try:
    # Get disk usage statistics for '/web'
    disk = disk_usage("/")
    web_total, web_used, web_free = disk["total"], disk["used"], disk["free"]
    print(web_total, web_used, web_free)
    free_percent = disk["free_percent"]

    data = {
        "total_space": web_total,
        "used_space": web_used,
        "free_space": web_free,
        "free_percent": free_percent
    }
    # Write data to diskCheck.json
    with open(sc_path("diskCheck.json"), "w") as json_file:
        json.dump(data, json_file)

    alert_sent = False
    # When disk free space is below the threshold, send an email to the admin
    if free_percent < ALERT_FREE_PERCENT:
        # Mailjet credentials come from ENV_FILE (MAILJET_API_KEY/_SECRET_KEY)
        alert_sent = send_alert(
            "Disk Space Alert",
            f"Warning: Disk space is below {ALERT_FREE_PERCENT}%. Current free space: {free_percent:.2f}%.",
            channels=("mailjet",),
            email_from="g.otterspeer@uva.nl", from_name="Disk Monitor",
            email_to="g.otterspeer@uva.nl", to_name="Admin",
        )
        if alert_sent:
            print("Alert email sent successfully.")
        else:
            print("Failed to send alert email.")

    # Send success heartbeat with disk usage stats
    monitor.send_heartbeat_with_stats(
        status="success",
        message=f"Disk check completed. Free space: {free_percent:.2f}%",
        stats={
            "total_bytes": web_total,
            "used_bytes": web_used,
            "free_bytes": web_free,
            "free_percent": free_percent,
            "alert_sent": alert_sent
        }
    )

except Exception as e:
    # Send error heartbeat
    monitor.send_heartbeat_with_stats(
        status="error",
        message=f"Disk check failed: {str(e)}",
        stats={"error_type": type(e).__name__}
    )
    raise  # Re-raise to maintain existing error behavior
