#!/usr/bin/env python3
"""
Client Monitor API Library

Provides the ClientMonitor class for integrating with the Client Monitor API
to track script execution, send heartbeats, and report statistics.

Usage:
    from python_client import ClientMonitor

    monitor = ClientMonitor(
        client_id="my-service",
        client_name="My Service",
        description="Service description",
        heartbeat_interval=3600
    )

    # Register first (done automatically in __init__)
    # Then send heartbeats after script execution
    monitor.send_heartbeat_with_stats(
        status="success",
        message="Operation completed",
        stats={"files_processed": 42}
    )
"""

import requests
import socket
import json
import sys
from datetime import datetime
from typing import Dict, Any, Optional


class ClientMonitor:
    """
    Client Monitor API integration class.

    Handles registration and heartbeat reporting to the Client Monitor API.
    All network errors are caught and logged to prevent monitoring failures
    from disrupting the main script.
    """

    def __init__(
        self,
        client_id: str,
        client_name: str,
        description: str = "",
        heartbeat_interval: int = 3600,
        api_url: str = "https://signcollect.nl/client_monitor_api/api.php"
    ):
        """
        Initialize the ClientMonitor.

        Args:
            client_id: Unique identifier for this client (e.g., "check-disk")
            client_name: Human-readable name (e.g., "Disk Space Monitor")
            description: Brief description of what this client does
            heartbeat_interval: Expected interval between heartbeats in seconds
            api_url: URL of the Client Monitor API endpoint
        """
        self.client_id = client_id
        self.client_name = client_name
        self.description = description
        self.heartbeat_interval = heartbeat_interval
        self.api_url = api_url
        self.hostname = socket.gethostname()

        # Automatically register on initialization
        self.register()

    def register(self, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Register this client with the API.

        Args:
            metadata: Optional custom metadata dictionary

        Returns:
            True if registration was successful, False otherwise
        """
        try:
            data = {
                "client_id": self.client_id,
                "client_name": self.client_name,
                "description": self.description,
                "heartbeat_interval": self.heartbeat_interval,
                "metadata": metadata or {
                    "hostname": self.hostname,
                    "python_version": sys.version.split()[0],
                    "registered_at": datetime.now().isoformat()
                }
            }

            # NOTE: action goes in the URL query parameter, not in the JSON body
            response = requests.post(
                f"{self.api_url}?action=register",
                json=data,
                headers={"Content-Type": "application/json"},
                timeout=10
            )

            # Accept both 200 (OK) and 201 (Created) as success
            if response.status_code in [200, 201]:
                result = response.json()
                if result.get("success"):
                    print(f"[ClientMonitor] Registered: {self.client_id}")
                    return True
                else:
                    print(f"[ClientMonitor] Registration failed: {result.get('errors')}")
                    return False
            else:
                # Try to parse the response to check if it's just a duplicate
                try:
                    result = response.json()
                    errors = result.get('errors', [])
                    # If client already exists, treat it as success (already registered)
                    if any('already exists' in str(err).lower() for err in errors):
                        print(f"[ClientMonitor] Client already registered: {self.client_id}")
                        return True
                except:
                    pass
                print(f"[ClientMonitor] Registration failed with status {response.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            print(f"[ClientMonitor] Registration error: {e}")
            return False
        except Exception as e:
            print(f"[ClientMonitor] Unexpected registration error: {e}")
            return False

    def send_heartbeat(self, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Send a heartbeat to update the last_seen timestamp.

        Args:
            metadata: Optional custom metadata to include with the heartbeat

        Returns:
            True if heartbeat was successful, False otherwise
        """
        try:
            data = {
                "client_id": self.client_id,
                "metadata": metadata or {
                    "last_run": datetime.now().isoformat(),
                    "hostname": self.hostname
                }
            }

            # NOTE: action goes in the URL query parameter, not in the JSON body
            response = requests.post(
                f"{self.api_url}?action=heartbeat",
                json=data,
                headers={"Content-Type": "application/json"},
                timeout=10
            )

            # Accept both 200 (OK) and 201 (Created) as success
            if response.status_code in [200, 201]:
                result = response.json()
                if result.get("success"):
                    return True
                else:
                    print(f"[ClientMonitor] Heartbeat failed: {result.get('errors')}")
                    return False
            else:
                print(f"[ClientMonitor] Heartbeat failed with status {response.status_code}")
                return False

        except requests.exceptions.RequestException as e:
            print(f"[ClientMonitor] Heartbeat error: {e}")
            return False
        except Exception as e:
            print(f"[ClientMonitor] Unexpected heartbeat error: {e}")
            return False

    def send_heartbeat_with_stats(
        self,
        status: str,
        message: str,
        stats: Optional[Dict[str, Any]] = None
    ) -> bool:
        """
        Send a heartbeat with status and statistics.

        Args:
            status: Status of the execution (e.g., 'success', 'error', 'warning')
            message: Description message
            stats: Optional statistics dictionary

        Returns:
            True if heartbeat was successful, False otherwise
        """
        metadata = {
            "last_run": datetime.now().isoformat(),
            "hostname": self.hostname,
            "status": status,
            "message": message
        }

        if stats:
            metadata.update(stats)

        return self.send_heartbeat(metadata)


# Example usage
if __name__ == "__main__":
    # Test the ClientMonitor class
    print("Testing ClientMonitor class...")

    monitor = ClientMonitor(
        client_id="test-client",
        client_name="Test Client",
        description="Testing the ClientMonitor library",
        heartbeat_interval=60
    )

    # Send a test heartbeat
    monitor.send_heartbeat_with_stats(
        status="success",
        message="Test heartbeat from python_client.py",
        stats={
            "test_value": 123,
            "test_string": "hello world"
        }
    )

    print("Test complete!")
