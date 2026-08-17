#with this script we can check the disk space of the system
#when the disk space is less than 20% write to diskCheck.json file

import subprocess
import json
import os
import requests
import sys
sys.path.insert(0, '/home/gomer/pythonCron')
from python_client import ClientMonitor

# Load credentials from /web/zin/.env (see README.md)
ENV_FILE = "/web/zin/.env"
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

# Function to get disk usage statistics using 'df -B1' with robust parsing
def get_disk_usage(path):
    try:
        df_output = subprocess.check_output(["df", "-B1", path, "--output=size,used,avail"]).decode("utf-8").split("\n")[1].split()
        if len(df_output) < 3:
            raise ValueError("Unexpected df output format")
        total = int(df_output[0])
        used = int(df_output[1])
        free = int(df_output[2])
        return total, used, free
    except (subprocess.CalledProcessError, IndexError, ValueError) as e:
        print(f"Error retrieving disk usage for {path}: {e}")
        return 0, 0, 0

try:
    # Get disk usage statistics for '/web'
    web_total, web_used, web_free = get_disk_usage("/")
    print(web_total, web_used, web_free)
    # Calculate percentage of free disk space
    free_percent = web_free / web_total * 100

    data = {
        "total_space": web_total,
        "used_space": web_used,
        "free_space": web_free,
        "free_percent": free_percent
    }
    # Write data to diskCheck.json
    with open("/web/diskCheck.json", "w") as json_file:
        json.dump(data, json_file)

    alert_sent = False
    # When disk free space is less than 10%, send an email to the admin
    if free_percent < 30:
        mailjet_api_key = os.getenv("MAILJET_API_KEY", "")
        mailjet_secret_key = os.getenv("MAILJET_SECRET_KEY", "")
        headers = {
            "Content-Type": "application/json"
        }
        email_data = {
            "Messages": [
                {
                    "From": {
                        "Email": "g.otterspeer@uva.nl",
                        "Name": "Disk Monitor"
                    },
                    "To": [
                        {
                            "Email": "g.otterspeer@uva.nl",
                            "Name": "Admin"
                        }
                    ],
                    "Subject": "Disk Space Alert",
                    "TextPart": f"Warning: Disk space is below 10%. Current free space: {free_percent:.2f}%."
                }
            ]
        }
        if not (mailjet_api_key and mailjet_secret_key):
            print(f"MAILJET_API_KEY/MAILJET_SECRET_KEY not set in {ENV_FILE}; skipping alert email.")
        else:
            response = requests.post(
                "https://api.mailjet.com/v3.1/send",
                json=email_data,
                auth=(mailjet_api_key, mailjet_secret_key),
                headers=headers
            )
            if response.status_code == 200:
                print("Alert email sent successfully.")
                alert_sent = True
            else:
                print(f"Failed to send alert email: {response.text}")
            print(response.text)

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
