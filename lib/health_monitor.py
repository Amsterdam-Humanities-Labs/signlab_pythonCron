"""
Health monitor for scheduler v2.
Actively monitors service health and kills stuck processes.
"""

import threading
import time
import os
import signal
import logging
import logging.handlers
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional, Dict, Any
import psutil

if TYPE_CHECKING:
    from .state_manager import StateManager

logger = logging.getLogger(__name__)


# 5 MB x 5, the same policy as setup_rotating_logger in the heartbeat client.
WATCHDOG_LOG_MAX_BYTES = 5 * 1024 * 1024
WATCHDOG_LOG_BACKUP_COUNT = 5


def watchdog_logger(path: str) -> logging.Logger:
    """The size-rotated writer for watchdog.log.

    The scheduler and the health monitor both append to this file; sharing one
    logger (and so one handler) per path keeps rotation safe between them.
    Lines keep the old `[YYYY-mm-dd HH:MM:SS] message` format.
    """
    wlog = logging.getLogger('pythoncron.watchdog.' + os.path.abspath(path))
    if not wlog.handlers:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=WATCHDOG_LOG_MAX_BYTES,
            backupCount=WATCHDOG_LOG_BACKUP_COUNT)
        handler.setFormatter(logging.Formatter(
            '[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
        wlog.addHandler(handler)
        wlog.setLevel(logging.INFO)
        wlog.propagate = False
    return wlog


class HealthMonitor(threading.Thread):
    """
    Background thread that monitors scheduler health and kills stuck processes.
    """

    # How long a service can be in 'running' state before considered stuck
    STUCK_THRESHOLD = timedelta(hours=3)

    # How often to check for stuck services (seconds)
    CHECK_INTERVAL = 60

    # How often to log heartbeat (seconds)
    HEARTBEAT_INTERVAL = 300  # 5 minutes

    # Maximum time without heartbeat before considered dead
    MAX_NO_HEARTBEAT = timedelta(minutes=15)

    def __init__(self, state_manager: 'StateManager', watchdog_log_path: str = None):
        """
        Initialize the health monitor.

        Args:
            state_manager: StateManager instance
            watchdog_log_path: Path to the watchdog log file
        """
        super().__init__(daemon=True, name='HealthMonitor')
        self.state_manager = state_manager
        self.watchdog_log_path = watchdog_log_path or os.path.join(
            os.environ.get('PYTHONCRON_STATE_DIR')
            or os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'watchdog.log'
        )
        self._stop_event = threading.Event()
        self._last_heartbeat = datetime.now()
        self._heartbeat_lock = threading.Lock()

    def run(self):
        """Main monitoring loop."""
        last_heartbeat_log = datetime.now() - timedelta(seconds=self.HEARTBEAT_INTERVAL)

        while not self._stop_event.is_set():
            try:
                # Check for stuck services
                self._check_stuck_services()

                # Check for expired circuit breakers
                self._check_expired_circuits()

                # Log heartbeat periodically
                now = datetime.now()
                if (now - last_heartbeat_log).total_seconds() >= self.HEARTBEAT_INTERVAL:
                    self._log_heartbeat()
                    last_heartbeat_log = now

                # Clean up old history
                self._cleanup_old_data()

            except Exception as e:
                logger.error(f"Error in health monitor loop: {e}", exc_info=True)
                self._log_watchdog(f"ERROR in health monitor: {e}")

            # Wait before next check
            self._stop_event.wait(self.CHECK_INTERVAL)

    def stop(self):
        """Signal the monitor to stop."""
        self._stop_event.set()

    def update_heartbeat(self):
        """Update the heartbeat timestamp (called from main loop)."""
        with self._heartbeat_lock:
            self._last_heartbeat = datetime.now()

    def check_main_loop_alive(self) -> bool:
        """Check if the main loop is still alive."""
        with self._heartbeat_lock:
            time_since_heartbeat = datetime.now() - self._last_heartbeat
            return time_since_heartbeat < self.MAX_NO_HEARTBEAT

    def _check_stuck_services(self):
        """Find and kill services stuck in 'running' state."""
        stuck_services = self.state_manager.get_stuck_services(self.STUCK_THRESHOLD)

        for service in stuck_services:
            service_name = service['service_name']
            pid = service.get('pid')
            start_time = service.get('last_execution_start')

            logger.warning(
                f"Service '{service_name}' stuck in running state "
                f"(PID: {pid}, started: {start_time})"
            )
            self._log_watchdog(
                f"STUCK SERVICE: '{service_name}' (PID: {pid}, started: {start_time})"
            )

            # Kill the process
            self._kill_and_reset(service)

    def _kill_and_reset(self, service: Dict[str, Any]):
        """Kill a stuck process and mark as failed."""
        service_name = service['service_name']
        pid = service.get('pid')

        if pid:
            try:
                # Try to kill the process group
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                    logger.info(f"Sent SIGTERM to process group of PID {pid}")
                except (ProcessLookupError, PermissionError):
                    pass

                # Wait a bit, then force kill if still running
                time.sleep(5)

                if psutil.pid_exists(pid):
                    try:
                        process = psutil.Process(pid)
                        if process.is_running():
                            os.killpg(os.getpgid(pid), signal.SIGKILL)
                            logger.info(f"Sent SIGKILL to process group of PID {pid}")
                    except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
                        pass

            except ProcessLookupError:
                logger.info(f"Process {pid} already terminated")
            except Exception as e:
                logger.error(f"Error killing process {pid}: {e}")

        # Mark as failed
        self.state_manager.mark_failed(
            service_name,
            error=f"Killed: stuck in running state for > {self.STUCK_THRESHOLD}"
        )

        self._log_watchdog(
            f"KILLED stuck service '{service_name}' (PID: {pid})"
        )

    def _check_expired_circuits(self):
        """Check for circuit breakers that have expired their backoff."""
        # The circuit breaker handles this during is_open() checks,
        # but we can proactively close expired circuits here
        services = self.state_manager.get_services_by_status(
            self.state_manager.STATUS_CIRCUIT_OPEN
        )

        now = datetime.now()
        for service in services:
            circuit_open_until = service.get('circuit_open_until')
            if circuit_open_until:
                try:
                    open_until = datetime.strptime(circuit_open_until, '%Y-%m-%d %H:%M:%S')
                    if now >= open_until:
                        service_name = service['service_name']
                        self.state_manager.close_circuit(service_name)
                        logger.info(
                            f"Circuit breaker reset for '{service_name}' - backoff expired"
                        )
                except ValueError:
                    pass

    def _cleanup_old_data(self):
        """Periodically clean up old execution history."""
        # Only run cleanup occasionally (check every hour)
        if not hasattr(self, '_last_cleanup'):
            self._last_cleanup = datetime.now()

        if (datetime.now() - self._last_cleanup).total_seconds() >= 3600:
            try:
                deleted = self.state_manager.cleanup_old_history(days=30)
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old history records")
                self._last_cleanup = datetime.now()
            except Exception as e:
                logger.error(f"Error cleaning up old history: {e}")

    def _log_heartbeat(self):
        """Log detailed heartbeat with system stats."""
        try:
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            cpu_percent = process.cpu_percent(interval=0.1)
        except Exception:
            memory_mb = 0
            cpu_percent = 0

        # Count services by status
        all_services = self.state_manager.get_all_services()
        status_counts = {}
        for svc in all_services:
            status = svc.get('status', 'unknown')
            status_counts[status] = status_counts.get(status, 0) + 1

        # Get stuck count
        stuck_count = len(self.state_manager.get_stuck_services(self.STUCK_THRESHOLD))

        # Get active thread count
        active_threads = threading.active_count()

        msg = (
            f"HEARTBEAT: Scheduler alive | "
            f"Threads: {active_threads} | "
            f"Memory: {memory_mb:.1f}MB | "
            f"CPU: {cpu_percent:.1f}% | "
            f"Services: {status_counts} | "
            f"Stuck: {stuck_count}"
        )

        logger.info(msg)
        self._log_watchdog(msg)

    def _log_watchdog(self, message: str):
        """Write to the watchdog log file."""
        try:
            watchdog_logger(self.watchdog_log_path).info(message)
        except Exception as e:
            logger.error(f"Failed to write to watchdog log: {e}")

    def get_health_status(self) -> Dict[str, Any]:
        """
        Get comprehensive health status.

        Returns:
            Dict with health status information
        """
        try:
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024
            cpu_percent = process.cpu_percent(interval=0.1)
        except Exception:
            memory_mb = 0
            cpu_percent = 0

        all_services = self.state_manager.get_all_services()
        stuck_services = self.state_manager.get_stuck_services(self.STUCK_THRESHOLD)

        status_counts = {}
        for svc in all_services:
            status = svc.get('status', 'unknown')
            status_counts[status] = status_counts.get(status, 0) + 1

        main_loop_alive = self.check_main_loop_alive()

        return {
            'healthy': main_loop_alive and len(stuck_services) == 0,
            'main_loop_alive': main_loop_alive,
            'last_heartbeat': self._last_heartbeat.strftime('%Y-%m-%d %H:%M:%S'),
            'active_threads': threading.active_count(),
            'memory_mb': memory_mb,
            'cpu_percent': cpu_percent,
            'total_services': len(all_services),
            'status_counts': status_counts,
            'stuck_services': len(stuck_services),
            'stuck_service_names': [s['service_name'] for s in stuck_services],
        }
