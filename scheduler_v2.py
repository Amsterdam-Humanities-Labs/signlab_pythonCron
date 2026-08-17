#!/usr/bin/env python3
"""
Scheduler v2 - A robust service scheduler with proper state management.

Features:
- SQLite-based state management with proper locking
- Circuit breaker pattern to prevent infinite retries
- Active health monitoring with stuck process detection
- Proper service name sanitization
- Graceful shutdown handling
"""

import json
import signal
import sys
import time
import logging
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from lib.state_manager import StateManager
from lib.circuit_breaker import CircuitBreaker
from lib.health_monitor import HealthMonitor
from lib.service_executor import ServiceExecutor
from lib.config_validator import ConfigValidator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/home/gomer/pythonCron/scheduler_v2.log')
    ]
)
logger = logging.getLogger('scheduler_v2')

# Configuration paths
DEFAULT_CONFIG_PATH = '/home/gomer/pythonCron/config.json'
DEFAULT_DB_PATH = '/home/gomer/pythonCron/scheduler_state.db'
DEFAULT_WATCHDOG_LOG = '/home/gomer/pythonCron/watchdog.log'

# Timing constants
MAIN_LOOP_INTERVAL = 60  # seconds
FAILED_RECOVERY_HOUR = 6  # Hour to auto-recover failed services


class Scheduler:
    """
    Main scheduler class that coordinates all components.
    """

    def __init__(
        self,
        config_path: str = DEFAULT_CONFIG_PATH,
        db_path: str = DEFAULT_DB_PATH,
        watchdog_log: str = DEFAULT_WATCHDOG_LOG,
        dry_run: bool = False
    ):
        """
        Initialize the scheduler.

        Args:
            config_path: Path to config.json
            db_path: Path to SQLite database
            watchdog_log: Path to watchdog log file
            dry_run: If True, don't actually execute services
        """
        self.config_path = config_path
        self.db_path = db_path
        self.watchdog_log = watchdog_log
        self.dry_run = dry_run
        self.running = False
        self._config = []
        self._failed_recovery_done = False

        # Initialize components
        logger.info("Initializing scheduler components...")

        self.state_manager = StateManager(db_path)
        self.circuit_breaker = CircuitBreaker(self.state_manager)
        self.executor = ServiceExecutor(
            self.state_manager,
            self.circuit_breaker,
            max_workers=20
        )
        self.health_monitor = HealthMonitor(
            self.state_manager,
            watchdog_log
        )

        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info("Scheduler initialized")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self._log_watchdog(f"Received signal {signum}, shutting down...")
        self.running = False

    def _log_watchdog(self, message: str):
        """Write to watchdog log."""
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(self.watchdog_log, 'a') as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception as e:
            logger.error(f"Failed to write to watchdog log: {e}")

    def _load_and_validate_config(self) -> List[Dict[str, Any]]:
        """Load and validate configuration."""
        logger.info(f"Loading configuration from {self.config_path}")

        validator = ConfigValidator(self.config_path)

        try:
            validator.load_config()
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            self._log_watchdog(f"ERROR: Failed to load config: {e}")
            return []

        is_valid, errors, warnings = validator.validate()

        for error in errors:
            logger.error(f"Config error: {error}")

        for warning in warnings:
            logger.warning(f"Config warning: {warning}")

        # Get valid services only
        valid_services = validator.get_valid_services()
        logger.info(f"Loaded {len(valid_services)} valid services")

        return valid_services

    def _sync_services_with_db(self, config: List[Dict[str, Any]]):
        """Sync database with configuration."""
        service_names = [svc['service_name'] for svc in config]
        self.state_manager.sync_services(service_names)
        logger.info(f"Synced {len(service_names)} services with database")

    def _should_execute_service(
        self,
        service: Dict[str, Any],
        current_time: datetime
    ) -> bool:
        """
        Determine if a service should be executed now.

        Args:
            service: Service configuration
            current_time: Current datetime

        Returns:
            True if service should be executed
        """
        service_name = service['service_name']
        time_or_minute = service.get('time_or_minute', 'minute')
        interval_minutes = service.get('interval_minutes', 60)
        scheduled_time = service.get('scheduled_time')

        # Get fresh status from database
        status = self.state_manager.get_service_status(service_name)

        # If service is currently running, skip
        if status and status.get('status') == self.state_manager.STATUS_RUNNING:
            return False

        # If circuit breaker is open, skip
        if not self.circuit_breaker.can_execute(service_name):
            return False

        # Get last execution time
        last_execution = None
        if status and status.get('last_execution_end'):
            try:
                last_execution = datetime.strptime(
                    status['last_execution_end'],
                    '%Y-%m-%d %H:%M:%S'
                )
            except ValueError:
                pass

        if time_or_minute == 'minute':
            # Interval-based scheduling
            if last_execution is None:
                # Never executed, should run
                return True

            next_run = last_execution + timedelta(minutes=interval_minutes)
            return current_time >= next_run

        elif time_or_minute == 'time':
            # Time-based scheduling
            if not scheduled_time:
                return False

            try:
                scheduled_dt = datetime.strptime(scheduled_time, "%H:%M")
                scheduled_full = datetime.combine(
                    current_time.date(),
                    scheduled_dt.time()
                )

                # Check if we're within 2 minutes of scheduled time
                time_diff = abs((current_time - scheduled_full).total_seconds())
                if time_diff > 120:  # More than 2 minutes away
                    return False

                # Check if already executed recently
                if last_execution:
                    time_since_execution = current_time - last_execution
                    if time_since_execution < timedelta(minutes=interval_minutes):
                        return False

                return True

            except ValueError:
                return False

        return False

    def _process_due_services(self, config: List[Dict[str, Any]]):
        """Process all services that are due for execution."""
        current_time = datetime.now()

        for service in config:
            service_name = service['service_name']

            try:
                if self._should_execute_service(service, current_time):
                    if self.dry_run:
                        logger.info(f"[DRY RUN] Would execute: {service_name}")
                    else:
                        logger.info(f"Scheduling execution: {service_name}")
                        self.executor.execute_service(service)

            except Exception as e:
                logger.error(f"Error checking service '{service_name}': {e}")

    def _execute_failed_services(self, config: List[Dict[str, Any]]):
        """Execute all failed services (for daily recovery)."""
        logger.info("Executing failed services for recovery...")

        failed_services = self.state_manager.get_services_by_status(
            self.state_manager.STATUS_FAILED
        )

        config_map = {svc['service_name']: svc for svc in config}

        for svc in failed_services:
            service_name = svc['service_name']
            if service_name in config_map:
                service_config = config_map[service_name]

                # Skip if circuit breaker is open
                if not self.circuit_breaker.can_execute(service_name):
                    logger.info(
                        f"Skipping '{service_name}' - circuit breaker open"
                    )
                    continue

                if self.dry_run:
                    logger.info(f"[DRY RUN] Would retry failed: {service_name}")
                else:
                    logger.info(f"Retrying failed service: {service_name}")
                    self.executor.execute_service(service_config)

    def run(self):
        """Main scheduler loop."""
        logger.info("=" * 60)
        logger.info("SCHEDULER V2 STARTING")
        logger.info("=" * 60)
        self._log_watchdog("SCHEDULER V2 STARTING")

        # Load and validate config
        self._config = self._load_and_validate_config()
        if not self._config:
            logger.error("No valid services configured, exiting")
            return

        # Sync with database
        self._sync_services_with_db(self._config)

        # Start health monitor
        self.health_monitor.start()

        self.running = True
        logger.info("Starting main loop...")

        while self.running:
            try:
                # Update health monitor heartbeat
                self.health_monitor.update_heartbeat()

                # Process due services
                self._process_due_services(self._config)

                # Handle daily failed service recovery (6:00-6:30 AM)
                now = datetime.now()
                if now.hour == FAILED_RECOVERY_HOUR and 0 <= now.minute <= 30:
                    if not self._failed_recovery_done:
                        self._execute_failed_services(self._config)
                        self._failed_recovery_done = True
                elif now.hour == FAILED_RECOVERY_HOUR and now.minute > 30:
                    self._failed_recovery_done = False

                # Sleep until next check
                time.sleep(MAIN_LOOP_INTERVAL)

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                self._log_watchdog(f"ERROR in main loop: {e}")
                time.sleep(5)  # Brief pause before continuing

        # Graceful shutdown
        self._shutdown()

    def _shutdown(self):
        """Perform graceful shutdown."""
        logger.info("Shutting down scheduler...")
        self._log_watchdog("Scheduler shutting down...")

        # Stop health monitor
        self.health_monitor.stop()

        # Shutdown executor
        self.executor.shutdown(wait=True)

        # Close database connection
        self.state_manager.close()

        logger.info("Scheduler stopped")
        self._log_watchdog("Scheduler stopped")

    def interactive_menu(self):
        """Interactive menu for manual operations."""
        while True:
            print("\n=== Scheduler v2 Menu ===")
            print("1. View service statuses")
            print("2. Execute failed services")
            print("3. View circuit breaker status")
            print("4. Force close circuit breaker for service")
            print("5. View health status")
            print("6. Exit")

            choice = input("Enter choice (1-6): ").strip()

            if choice == '1':
                self._show_service_statuses()
            elif choice == '2':
                self._execute_failed_services(self._config)
            elif choice == '3':
                self._show_circuit_breakers()
            elif choice == '4':
                self._force_close_circuit()
            elif choice == '5':
                self._show_health_status()
            elif choice == '6':
                break
            else:
                print("Invalid choice")

    def _show_service_statuses(self):
        """Display all service statuses."""
        services = self.state_manager.get_all_services()

        print("\n=== Service Statuses ===")
        for svc in services:
            print(f"\nService: {svc['service_name']}")
            print(f"  Status: {svc.get('status', 'unknown')}")
            print(f"  Last execution: {svc.get('last_execution_end', 'Never')}")
            print(f"  Failures: {svc.get('consecutive_failures', 0)}")
            if svc.get('last_error'):
                print(f"  Last error: {svc['last_error'][:100]}...")

    def _show_circuit_breakers(self):
        """Display circuit breaker statuses."""
        open_circuits = self.circuit_breaker.get_all_open_circuits()

        print("\n=== Open Circuit Breakers ===")
        if not open_circuits:
            print("No circuits are currently open")
        else:
            for circuit in open_circuits:
                print(f"\nService: {circuit['service_name']}")
                print(f"  Failures: {circuit['failures']}")
                print(f"  Open until: {circuit['open_until']}")
                print(f"  Remaining: {circuit['remaining_seconds']:.0f}s")

    def _force_close_circuit(self):
        """Force close a circuit breaker."""
        service_name = input("Enter service name: ").strip()
        if service_name:
            self.circuit_breaker.force_close(service_name)
            print(f"Circuit breaker closed for '{service_name}'")

    def _show_health_status(self):
        """Display health status."""
        status = self.health_monitor.get_health_status()

        print("\n=== Health Status ===")
        print(f"Healthy: {status['healthy']}")
        print(f"Main loop alive: {status['main_loop_alive']}")
        print(f"Last heartbeat: {status['last_heartbeat']}")
        print(f"Active threads: {status['active_threads']}")
        print(f"Memory: {status['memory_mb']:.1f} MB")
        print(f"CPU: {status['cpu_percent']:.1f}%")
        print(f"Service counts: {status['status_counts']}")
        if status['stuck_service_names']:
            print(f"Stuck services: {status['stuck_service_names']}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Scheduler v2')
    parser.add_argument(
        '--config',
        default=DEFAULT_CONFIG_PATH,
        help='Path to config.json'
    )
    parser.add_argument(
        '--db',
        default=DEFAULT_DB_PATH,
        help='Path to SQLite database'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Do not actually execute services'
    )
    parser.add_argument(
        '--validate-config',
        action='store_true',
        help='Only validate configuration and exit'
    )
    parser.add_argument(
        '--interactive',
        action='store_true',
        help='Run in interactive mode'
    )

    args = parser.parse_args()

    # Validate config only
    if args.validate_config:
        validator = ConfigValidator(args.config)
        is_valid = validator.print_validation_report()
        sys.exit(0 if is_valid else 1)

    # Create scheduler
    scheduler = Scheduler(
        config_path=args.config,
        db_path=args.db,
        dry_run=args.dry_run
    )

    # Interactive mode
    if args.interactive:
        scheduler._config = scheduler._load_and_validate_config()
        scheduler.interactive_menu()
        return

    # Normal operation
    scheduler.run()


if __name__ == '__main__':
    main()
