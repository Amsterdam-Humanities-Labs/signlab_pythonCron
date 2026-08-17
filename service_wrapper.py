#!/usr/bin/env python3
"""
Service Wrapper - Self-Scheduling Service Executor

This wrapper runs a service in a loop based on configuration from services_config.json.
It handles scheduling, execution, timeouts, retries, logging, and health reporting.

Usage:
    python3 service_wrapper.py <service_name>

Example:
    python3 service_wrapper.py Check_Disk
"""

import json
import subprocess
import os
import sys
import time
import signal
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler
import traceback
import random

class ServiceWrapper:
    def __init__(self, service_name, config_path='/home/gomer/pythonCron/services_config.json'):
        self.service_name = service_name
        self.config_path = config_path
        self.config = None
        self.global_config = None
        self.logger = None
        self.state_file = None
        self.running = True
        self.current_process = None
        self.consecutive_failures = 0
        self.last_execution = None
        self.last_success = None

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        self.logger.info(f"Received signal {signum}. Shutting down gracefully...")
        self.running = False
        if self.current_process:
            try:
                self.logger.info(f"Terminating running process (PID {self.current_process.pid})")
                self.current_process.terminate()
                self.current_process.wait(timeout=10)
            except:
                self.current_process.kill()
        sys.exit(0)

    def load_config(self):
        """Load service configuration from services_config.json"""
        try:
            with open(self.config_path, 'r') as f:
                config_data = json.load(f)

            self.global_config = config_data.get('global', {})

            # Find the service configuration
            for service in config_data.get('services', []):
                if service['name'] == self.service_name:
                    self.config = service
                    break

            if not self.config:
                raise ValueError(f"Service '{self.service_name}' not found in configuration")

            if not self.config.get('enabled', True):
                raise ValueError(f"Service '{self.service_name}' is disabled in configuration")

            return True

        except Exception as e:
            print(f"ERROR: Failed to load configuration: {e}")
            traceback.print_exc()
            return False

    def setup_logging(self):
        """Set up logging with rotation"""
        log_dir = self.global_config.get('log_dir', '/home/gomer/pythonCron/logs')
        os.makedirs(log_dir, exist_ok=True)

        log_file = os.path.join(log_dir, f"{self.service_name}.log")

        # Get logging configuration
        log_config = self.config.get('logging', {})
        max_bytes = log_config.get('max_size_mb', 50) * 1024 * 1024
        backup_count = log_config.get('max_files', 3)
        log_level = getattr(logging, log_config.get('level', 'INFO'))

        # Create logger
        self.logger = logging.getLogger(self.service_name)
        self.logger.setLevel(log_level)

        # Create rotating file handler
        handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count
        )

        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)

        # Add handler to logger
        self.logger.addHandler(handler)

        # Also log to console
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        self.logger.info(f"Service wrapper initialized for '{self.service_name}'")

    def setup_state_file(self):
        """Set up state file for health reporting"""
        state_dir = self.global_config.get('state_dir', '/home/gomer/pythonCron/state')
        os.makedirs(state_dir, exist_ok=True)

        self.state_file = os.path.join(state_dir, f"{self.service_name}.json")

        # Initialize state
        self._update_state({
            'service': self.service_name,
            'status': 'starting',
            'pid': os.getpid(),
            'started_at': datetime.now().isoformat(),
            'last_execution': None,
            'last_success': None,
            'last_failure': None,
            'consecutive_failures': 0,
            'total_executions': 0,
            'total_successes': 0,
            'total_failures': 0
        })

    def _update_state(self, updates):
        """Update state file with new information"""
        try:
            # Read current state if exists
            state = {}
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)

            # Update with new data
            state.update(updates)
            state['updated_at'] = datetime.now().isoformat()

            # Write atomically using temp file
            temp_file = self.state_file + '.tmp'
            with open(temp_file, 'w') as f:
                json.dump(state, f, indent=2)
            os.replace(temp_file, self.state_file)

        except Exception as e:
            self.logger.error(f"Failed to update state file: {e}")

    def get_next_execution_time(self):
        """Calculate next execution time based on configuration"""
        exec_config = self.config['execution']
        exec_type = exec_config['type']

        now = datetime.now()

        if exec_type == 'interval':
            # Interval-based scheduling
            interval_seconds = exec_config['interval_seconds']
            jitter = random.randint(0, exec_config.get('jitter_seconds', 0))

            if self.last_execution:
                next_time = self.last_execution + timedelta(seconds=interval_seconds + jitter)
            else:
                # First run - execute immediately with small jitter
                next_time = now + timedelta(seconds=jitter)

            return next_time

        elif exec_type == 'time':
            # Time-based scheduling (specific time of day)
            scheduled_time_str = exec_config['time']  # Format: "HH:MM"
            hour, minute = map(int, scheduled_time_str.split(':'))

            # Create today's scheduled time
            scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

            # Add jitter
            jitter = random.randint(0, exec_config.get('jitter_seconds', 0))
            scheduled_today += timedelta(seconds=jitter)

            # If we've passed today's scheduled time, schedule for tomorrow
            if now >= scheduled_today:
                next_time = scheduled_today + timedelta(days=1)
            else:
                next_time = scheduled_today

            # Don't re-execute if we just ran within the interval
            interval_seconds = exec_config.get('interval_seconds', 86400)
            if self.last_execution and (now - self.last_execution).total_seconds() < interval_seconds / 2:
                # Skip to tomorrow's execution
                next_time = scheduled_today + timedelta(days=1)

            return next_time

        else:
            raise ValueError(f"Unknown execution type: {exec_type}")

    def execute_service(self):
        """Execute the service script with timeout and error handling"""
        cmd_config = self.config['command']
        executable = cmd_config['executable']
        script = cmd_config['script']
        working_dir = cmd_config['working_dir']
        timeout_seconds = cmd_config.get('timeout_seconds', 1800)
        environment = cmd_config.get('environment', {})

        self.logger.info(f"Executing service: {executable} {script}")
        self.logger.info(f"Working directory: {working_dir}")
        self.logger.info(f"Timeout: {timeout_seconds}s")

        # Update state to running
        self._update_state({
            'status': 'running',
            'last_execution': datetime.now().isoformat(),
            'total_executions': self._get_state_value('total_executions', 0) + 1
        })

        # Prepare environment
        env = os.environ.copy()
        env.update(environment)

        # Create temporary files for stdout/stderr to avoid pipe deadlocks
        stdout_file = None
        stderr_file = None

        try:
            stdout_file = tempfile.NamedTemporaryFile(mode='w+b', delete=False,
                                                     suffix=f'_{self.service_name}_stdout')
            stderr_file = tempfile.NamedTemporaryFile(mode='w+b', delete=False,
                                                     suffix=f'_{self.service_name}_stderr')

            # Start the process
            start_time = datetime.now()
            self.current_process = subprocess.Popen(
                [executable, script],
                cwd=working_dir,
                stdout=stdout_file,
                stderr=stderr_file,
                env=env,
                start_new_session=True
            )

            self.logger.info(f"Process started with PID {self.current_process.pid}")

            try:
                # Wait for process with timeout
                self.current_process.wait(timeout=timeout_seconds)

                # Close temp files
                stdout_file.close()
                stderr_file.close()

                # Read output
                with open(stdout_file.name, 'rb') as f:
                    stdout_data = f.read()
                with open(stderr_file.name, 'rb') as f:
                    stderr_data = f.read()

                stdout = stdout_data.decode('utf-8', errors='replace')
                stderr = stderr_data.decode('utf-8', errors='replace')

                # Clean up temp files
                os.unlink(stdout_file.name)
                os.unlink(stderr_file.name)

                execution_time = (datetime.now() - start_time).total_seconds()

                if self.current_process.returncode == 0:
                    self.logger.info(f"Service executed successfully (took {execution_time:.1f}s)")
                    if stdout:
                        self.logger.debug(f"STDOUT:\n{stdout}")
                    self.consecutive_failures = 0
                    self.last_success = datetime.now()

                    self._update_state({
                        'status': 'idle',
                        'last_success': self.last_success.isoformat(),
                        'consecutive_failures': 0,
                        'total_successes': self._get_state_value('total_successes', 0) + 1
                    })

                    return True

                else:
                    self.logger.error(f"Service failed with return code {self.current_process.returncode} (took {execution_time:.1f}s)")
                    if stdout:
                        self.logger.error(f"STDOUT:\n{stdout}")
                    if stderr:
                        self.logger.error(f"STDERR:\n{stderr}")

                    self.consecutive_failures += 1

                    self._update_state({
                        'status': 'failed',
                        'last_failure': datetime.now().isoformat(),
                        'consecutive_failures': self.consecutive_failures,
                        'total_failures': self._get_state_value('total_failures', 0) + 1
                    })

                    return False

            except subprocess.TimeoutExpired:
                self.logger.error(f"Service execution timed out after {timeout_seconds}s")

                # Kill the process group
                try:
                    os.killpg(os.getpgid(self.current_process.pid), signal.SIGTERM)
                    time.sleep(5)
                    if self.current_process.poll() is None:
                        os.killpg(os.getpgid(self.current_process.pid), signal.SIGKILL)
                except:
                    self.current_process.kill()

                try:
                    self.current_process.wait(timeout=10)
                except:
                    pass

                # Read partial output
                stdout_file.close()
                stderr_file.close()

                with open(stdout_file.name, 'rb') as f:
                    stdout_data = f.read()
                with open(stderr_file.name, 'rb') as f:
                    stderr_data = f.read()

                stdout = stdout_data.decode('utf-8', errors='replace')
                stderr = stderr_data.decode('utf-8', errors='replace')

                self.logger.error(f"Partial STDOUT:\n{stdout}")
                self.logger.error(f"Partial STDERR:\n{stderr}")

                # Clean up
                os.unlink(stdout_file.name)
                os.unlink(stderr_file.name)

                self.consecutive_failures += 1

                self._update_state({
                    'status': 'timeout',
                    'last_failure': datetime.now().isoformat(),
                    'consecutive_failures': self.consecutive_failures,
                    'total_failures': self._get_state_value('total_failures', 0) + 1
                })

                return False

        except Exception as e:
            self.logger.error(f"Exception during service execution: {e}")
            self.logger.error(traceback.format_exc())

            self.consecutive_failures += 1

            self._update_state({
                'status': 'error',
                'last_failure': datetime.now().isoformat(),
                'last_error': str(e),
                'consecutive_failures': self.consecutive_failures,
                'total_failures': self._get_state_value('total_failures', 0) + 1
            })

            # Clean up temp files
            try:
                if stdout_file:
                    stdout_file.close()
                    if hasattr(stdout_file, 'name') and os.path.exists(stdout_file.name):
                        os.unlink(stdout_file.name)
                if stderr_file:
                    stderr_file.close()
                    if hasattr(stderr_file, 'name') and os.path.exists(stderr_file.name):
                        os.unlink(stderr_file.name)
            except:
                pass

            return False

        finally:
            self.current_process = None

    def execute_with_retry(self):
        """Execute service with retry logic"""
        retry_config = self.config.get('retry', {})
        max_retries = retry_config.get('max_retries', 3)
        retry_delay = retry_config.get('retry_delay_seconds', 60)
        exponential_backoff = retry_config.get('exponential_backoff', True)

        for attempt in range(max_retries + 1):
            if attempt > 0:
                delay = retry_delay * (2 ** (attempt - 1)) if exponential_backoff else retry_delay
                self.logger.info(f"Retry attempt {attempt}/{max_retries} after {delay}s delay")
                time.sleep(delay)

            success = self.execute_service()
            if success:
                return True

        return False

    def _get_state_value(self, key, default=None):
        """Get a value from the current state file"""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    return state.get(key, default)
        except:
            pass
        return default

    def check_health_limits(self):
        """Check if service has exceeded failure limits"""
        health_config = self.config.get('health', {})
        max_consecutive_failures = health_config.get('max_consecutive_failures', 5)
        restart_delay = health_config.get('restart_delay_seconds', 300)

        if self.consecutive_failures >= max_consecutive_failures:
            self.logger.warning(f"Service has {self.consecutive_failures} consecutive failures (limit: {max_consecutive_failures})")
            self.logger.warning(f"Waiting {restart_delay}s before next attempt")
            time.sleep(restart_delay)
            self.consecutive_failures = 0  # Reset counter after delay

    def run(self):
        """Main service loop"""
        if not self.load_config():
            return 1

        self.setup_logging()
        self.setup_state_file()

        self.logger.info(f"Starting service wrapper for '{self.service_name}'")
        self.logger.info(f"Configuration: {json.dumps(self.config, indent=2)}")

        while self.running:
            try:
                # Calculate next execution time
                next_execution = self.get_next_execution_time()
                now = datetime.now()

                if now >= next_execution:
                    # Time to execute
                    self.last_execution = now
                    success = self.execute_with_retry()

                    # Check health limits
                    self.check_health_limits()

                else:
                    # Wait until next execution
                    wait_seconds = (next_execution - now).total_seconds()
                    self.logger.info(f"Next execution scheduled for {next_execution.strftime('%Y-%m-%d %H:%M:%S')} ({wait_seconds:.0f}s)")

                    # Update state to idle
                    self._update_state({
                        'status': 'idle',
                        'next_execution': next_execution.isoformat()
                    })

                    # Sleep in small intervals to allow for graceful shutdown
                    sleep_interval = min(wait_seconds, 60)
                    time.sleep(sleep_interval)

            except KeyboardInterrupt:
                self.logger.info("Received keyboard interrupt. Shutting down...")
                break

            except Exception as e:
                self.logger.error(f"Unexpected error in main loop: {e}")
                self.logger.error(traceback.format_exc())

                self._update_state({
                    'status': 'error',
                    'last_error': str(e)
                })

                # Sleep before retrying
                time.sleep(60)

        # Clean shutdown
        self.logger.info("Service wrapper shutting down")
        self._update_state({
            'status': 'stopped',
            'stopped_at': datetime.now().isoformat()
        })

        return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 service_wrapper.py <service_name>")
        print("Example: python3 service_wrapper.py Check_Disk")
        return 1

    service_name = sys.argv[1]
    wrapper = ServiceWrapper(service_name)
    return wrapper.run()


if __name__ == '__main__':
    sys.exit(main())
