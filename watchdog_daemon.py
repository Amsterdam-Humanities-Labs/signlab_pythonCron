#!/usr/bin/env python3
"""
Watchdog Daemon - Monitors and manages service wrapper processes

This daemon periodically checks that all enabled services are running properly.
It auto-restarts crashed services, cleans up zombies, and detects stuck processes.

Usage:
    python3 watchdog_daemon.py [--check-interval SECONDS] [--once]

Arguments:
    --check-interval SECONDS   Time between checks (default: 3600 seconds / 1 hour)
    --once                     Run once and exit (useful for cron)
"""

import json
import os
import sys
import time
import signal
import subprocess
import psutil
import logging
from datetime import datetime, timedelta
from pathlib import Path
import traceback
import argparse

# Log setup: the installed package, else the copy vendored beside this script
# (see README.md), else the stdlib. Both names need client 1.1.0+.
try:
    from signlab_client_monitor import disk_usage, setup_rotating_logger
except ImportError:
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from python_client import disk_usage, setup_rotating_logger
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

        def disk_usage(path="/"):
            return {"used_percent": psutil.disk_usage(path).percent}

class WatchdogDaemon:
    def __init__(self, config_path='/home/gomer/pythonCron/services_config.json',
                 check_interval=3600):
        self.config_path = config_path
        self.check_interval = check_interval
        self.config = None
        self.global_config = None
        self.logger = None
        self.running = True
        self.wrapper_script = '/home/gomer/pythonCron/service_wrapper.py'

        # Statistics
        self.stats = {
            'checks_performed': 0,
            'services_restarted': 0,
            'zombies_cleaned': 0,
            'stuck_processes_killed': 0
        }

        # Set up signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        self.logger.info(f"Received signal {signum}. Shutting down gracefully...")
        self.running = False
        sys.exit(0)

    def setup_logging(self):
        """Set up logging for the watchdog"""
        log_file = '/home/gomer/pythonCron/watchdog_daemon.log'

        # 100 MB x 3, and stdout
        self.logger = setup_rotating_logger(
            log_file, name='watchdog_daemon',
            max_bytes=100 * 1024 * 1024, backup_count=3, stream=sys.stdout,
            fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S')

        self.logger.info("=" * 80)
        self.logger.info("Watchdog daemon starting")
        self.logger.info(f"Check interval: {self.check_interval}s")

    def load_config(self):
        """Load configuration from services_config.json"""
        try:
            with open(self.config_path, 'r') as f:
                config_data = json.load(f)

            self.global_config = config_data.get('global', {})
            self.config = config_data

            enabled_services = [s for s in config_data.get('services', []) if s.get('enabled', True)]
            self.logger.info(f"Loaded configuration with {len(enabled_services)} enabled services")

            return True

        except Exception as e:
            self.logger.error(f"Failed to load configuration: {e}")
            traceback.print_exc()
            return False

    def get_enabled_services(self):
        """Get list of enabled services from configuration"""
        return [s for s in self.config.get('services', []) if s.get('enabled', True)]

    def check_disk_space(self):
        """Check disk space and log warnings"""
        try:
            used_percent = round(disk_usage('/')['used_percent'], 1)  # psutil's percent

            threshold_warning = self.global_config.get('disk_space_warning_threshold_percent', 85)
            threshold_critical = self.global_config.get('disk_space_critical_threshold_percent', 95)

            if used_percent >= threshold_critical:
                self.logger.critical(f"CRITICAL: Disk space at {used_percent}% (threshold: {threshold_critical}%)")
                return 'critical'
            elif used_percent >= threshold_warning:
                self.logger.warning(f"WARNING: Disk space at {used_percent}% (threshold: {threshold_warning}%)")
                return 'warning'
            else:
                self.logger.info(f"Disk space OK: {used_percent}% used")
                return 'ok'

        except Exception as e:
            self.logger.error(f"Failed to check disk space: {e}")
            return 'unknown'

    def find_wrapper_process(self, service_name):
        """Find the wrapper process for a given service"""
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
                try:
                    cmdline = proc.info['cmdline']
                    if cmdline and len(cmdline) >= 2:
                        # Check if it's a python3 process running service_wrapper.py with our service name
                        if ('python3' in cmdline[0] or 'python' in cmdline[0]):
                            if 'service_wrapper.py' in ' '.join(cmdline):
                                if service_name in cmdline:
                                    return proc
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            return None

        except Exception as e:
            self.logger.error(f"Error finding wrapper process for {service_name}: {e}")
            return None

    def read_service_state(self, service_name):
        """Read service state file"""
        state_dir = self.global_config.get('state_dir', '/home/gomer/pythonCron/state')
        state_file = os.path.join(state_dir, f"{service_name}.json")

        if not os.path.exists(state_file):
            return None

        try:
            with open(state_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to read state file for {service_name}: {e}")
            return None

    def is_process_stuck(self, service_name, service_config, state):
        """Check if a process appears to be stuck"""
        if not state or state.get('status') != 'running':
            return False

        # Check if process has been running longer than expected
        last_exec_str = state.get('last_execution')
        if not last_exec_str:
            return False

        try:
            last_exec = datetime.fromisoformat(last_exec_str)
            timeout = service_config['command'].get('timeout_seconds', 1800)

            # Consider stuck if running for 2x the timeout
            stuck_threshold = timeout * 2
            running_time = (datetime.now() - last_exec).total_seconds()

            if running_time > stuck_threshold:
                self.logger.warning(f"Service {service_name} has been running for {running_time:.0f}s (threshold: {stuck_threshold}s)")
                return True

        except Exception as e:
            self.logger.error(f"Error checking if {service_name} is stuck: {e}")

        return False

    def kill_stuck_process(self, service_name, proc):
        """Kill a stuck process"""
        try:
            self.logger.warning(f"Killing stuck process for {service_name} (PID {proc.pid})")

            # Try graceful termination first
            proc.terminate()
            time.sleep(5)

            # Force kill if still running
            if proc.is_running():
                proc.kill()
                proc.wait(timeout=5)

            self.logger.info(f"Successfully killed stuck process for {service_name}")
            self.stats['stuck_processes_killed'] += 1
            return True

        except Exception as e:
            self.logger.error(f"Failed to kill stuck process for {service_name}: {e}")
            return False

    def start_service_wrapper(self, service_name):
        """Start a service wrapper process"""
        try:
            self.logger.info(f"Starting wrapper for service: {service_name}")

            # Start the wrapper in the background
            proc = subprocess.Popen(
                ['python3', self.wrapper_script, service_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )

            # Give it a moment to start
            time.sleep(2)

            # Verify it started
            if proc.poll() is None:
                self.logger.info(f"Successfully started wrapper for {service_name} (PID {proc.pid})")
                self.stats['services_restarted'] += 1
                return True
            else:
                self.logger.error(f"Wrapper for {service_name} exited immediately with code {proc.returncode}")
                return False

        except Exception as e:
            self.logger.error(f"Failed to start wrapper for {service_name}: {e}")
            traceback.print_exc()
            return False

    def cleanup_zombies(self):
        """Clean up zombie processes"""
        try:
            zombie_count = 0
            for proc in psutil.process_iter(['pid', 'status', 'name']):
                try:
                    if proc.info['status'] == psutil.STATUS_ZOMBIE:
                        self.logger.warning(f"Found zombie process: PID {proc.info['pid']} ({proc.info['name']})")
                        zombie_count += 1

                        # Try to reap it
                        try:
                            os.waitpid(proc.info['pid'], os.WNOHANG)
                            self.logger.info(f"Reaped zombie process {proc.info['pid']}")
                            self.stats['zombies_cleaned'] += 1
                        except:
                            pass

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if zombie_count > 0:
                self.logger.info(f"Found {zombie_count} zombie processes")

        except Exception as e:
            self.logger.error(f"Error during zombie cleanup: {e}")

    def check_service(self, service_config):
        """Check a single service and take action if needed"""
        service_name = service_config['name']
        watchdog_config = service_config.get('watchdog', {})

        if not watchdog_config.get('enabled', True):
            return

        # Find the wrapper process
        proc = self.find_wrapper_process(service_name)

        # Read service state
        state = self.read_service_state(service_name)

        if proc is None:
            # Wrapper process not running
            self.logger.warning(f"Service wrapper not running for: {service_name}")

            # Auto-restart if configured
            if watchdog_config.get('auto_restart', True):
                self.start_service_wrapper(service_name)
            else:
                self.logger.info(f"Auto-restart disabled for {service_name}, skipping")

        else:
            # Wrapper is running - check if it's healthy
            self.logger.info(f"Service {service_name} is running (PID {proc.pid})")

            # Check if stuck
            if watchdog_config.get('check_if_hung', True):
                hung_threshold = watchdog_config.get('hung_threshold_seconds', 3600)

                if self.is_process_stuck(service_name, service_config, state):
                    self.logger.error(f"Service {service_name} appears to be stuck!")

                    if watchdog_config.get('auto_restart', True):
                        # Kill and restart
                        if self.kill_stuck_process(service_name, proc):
                            time.sleep(2)
                            self.start_service_wrapper(service_name)

            # Check consecutive failures
            if state:
                consecutive_failures = state.get('consecutive_failures', 0)
                health_config = service_config.get('health', {})
                alert_threshold = health_config.get('alert_after_failures', 5)

                if consecutive_failures >= alert_threshold:
                    self.logger.error(f"Service {service_name} has {consecutive_failures} consecutive failures!")

    def perform_check(self):
        """Perform a complete health check of all services"""
        self.logger.info("=" * 80)
        self.logger.info(f"Starting health check #{self.stats['checks_performed'] + 1}")
        self.logger.info("=" * 80)

        # Check disk space first
        disk_status = self.check_disk_space()

        # Clean up zombies
        self.cleanup_zombies()

        # Get enabled services
        services = self.get_enabled_services()
        self.logger.info(f"Checking {len(services)} enabled services...")

        # Check each service
        for service_config in services:
            try:
                self.check_service(service_config)
            except Exception as e:
                self.logger.error(f"Error checking service {service_config['name']}: {e}")
                traceback.print_exc()

        # Update statistics
        self.stats['checks_performed'] += 1

        # Log summary
        self.logger.info("=" * 80)
        self.logger.info(f"Health check complete")
        self.logger.info(f"Statistics: {self.stats}")
        self.logger.info("=" * 80)

    def run(self, run_once=False):
        """Main watchdog loop"""
        self.setup_logging()

        if not self.load_config():
            return 1

        if run_once:
            self.logger.info("Running in single-check mode")
            self.perform_check()
            return 0

        self.logger.info("Running in daemon mode")

        while self.running:
            try:
                self.perform_check()

                # Reload configuration each check (allows updates without restart)
                self.load_config()

                # Sleep until next check
                self.logger.info(f"Next check in {self.check_interval}s")
                time.sleep(self.check_interval)

            except KeyboardInterrupt:
                self.logger.info("Received keyboard interrupt. Shutting down...")
                break

            except Exception as e:
                self.logger.error(f"Unexpected error in watchdog loop: {e}")
                traceback.print_exc()
                time.sleep(60)  # Sleep before retrying

        self.logger.info("Watchdog daemon shutting down")
        return 0


def main():
    parser = argparse.ArgumentParser(description='Watchdog daemon for service wrappers')
    parser.add_argument('--check-interval', type=int, default=3600,
                       help='Time between checks in seconds (default: 3600)')
    parser.add_argument('--once', action='store_true',
                       help='Run once and exit (useful for cron)')
    parser.add_argument('--config', type=str, default='/home/gomer/pythonCron/services_config.json',
                       help='Path to services configuration file')

    args = parser.parse_args()

    watchdog = WatchdogDaemon(
        config_path=args.config,
        check_interval=args.check_interval
    )

    return watchdog.run(run_once=args.once)


if __name__ == '__main__':
    sys.exit(main())
