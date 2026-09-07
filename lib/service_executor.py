"""
Service executor for scheduler v2.
Handles subprocess execution with proper error handling and state management.
"""

import subprocess
import os
import re
import signal
import time
import tempfile
import threading
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor

if TYPE_CHECKING:
    from .state_manager import StateManager
    from .circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


def sanitize_for_tempfile(service_name: str) -> str:
    """
    Replace characters that are invalid in filenames.
    Handles / \\ : * ? " < > | and whitespace.

    Args:
        service_name: Original service name

    Returns:
        Sanitized string safe for use in file paths
    """
    return re.sub(r'[/\\:*?"<>|\s]', '_', service_name)


def sanitize_for_log(service_name: str) -> str:
    """
    Sanitize service name for log file names.

    Args:
        service_name: Original service name

    Returns:
        Sanitized string safe for log file names
    """
    # Replace spaces with underscores
    name = service_name.replace(' ', '_')
    # Remove any character that is not alphanumeric, underscore, or hyphen
    name = re.sub(r'[^\w\-]', '', name)
    return f"{name}.log"


class ServiceExecutor:
    """
    Executes services with proper state management and error handling.
    """

    # Default timeout in seconds (30 minutes)
    DEFAULT_TIMEOUT = 1800

    # Maximum log file size (100MB)
    MAX_LOG_SIZE = 100 * 1024 * 1024

    def __init__(
        self,
        state_manager: 'StateManager',
        circuit_breaker: 'CircuitBreaker',
        log_dir: Optional[str] = None,
        max_workers: int = 20
    ):
        """
        Initialize the service executor.

        Args:
            state_manager: StateManager instance
            circuit_breaker: CircuitBreaker instance
            log_dir: Directory for service log files. Defaults to
                PYTHONCRON_STATE_DIR, else the directory holding this package -
                i.e. the repository root, which is where these logs have
                always been written.
            max_workers: Maximum concurrent service executions
        """
        self.state_manager = state_manager
        self.circuit_breaker = circuit_breaker
        self.log_dir = (
            log_dir
            or os.environ.get('PYTHONCRON_STATE_DIR')
            or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._running_lock = threading.Lock()
        self._shutdown = False

    def execute_service(self, service_config: Dict[str, Any]) -> bool:
        """
        Execute a single service.

        Args:
            service_config: Service configuration dict

        Returns:
            True if service started successfully, False otherwise
        """
        service_name = service_config['service_name']

        # Check circuit breaker FIRST (fresh read)
        if not self.circuit_breaker.can_execute(service_name):
            logger.info(f"Service '{service_name}' blocked by circuit breaker")
            return False

        # Atomic transition to running
        if not self.state_manager.transition_to_running(service_name):
            logger.info(f"Service '{service_name}' already running, skipping")
            return False

        # Submit for execution
        future = self._executor.submit(self._execute_service_impl, service_config)
        return True

    def execute_service_sync(self, service_config: Dict[str, Any]) -> bool:
        """
        Execute a service synchronously (blocking).

        Args:
            service_config: Service configuration dict

        Returns:
            True if execution succeeded, False otherwise
        """
        service_name = service_config['service_name']

        # Check circuit breaker
        if not self.circuit_breaker.can_execute(service_name):
            logger.info(f"Service '{service_name}' blocked by circuit breaker")
            return False

        # Atomic transition to running
        if not self.state_manager.transition_to_running(service_name):
            logger.info(f"Service '{service_name}' already running, skipping")
            return False

        return self._execute_service_impl(service_config)

    def _execute_service_impl(self, service_config: Dict[str, Any]) -> bool:
        """
        Internal implementation of service execution.

        Args:
            service_config: Service configuration dict

        Returns:
            True if execution succeeded, False otherwise
        """
        service_name = service_config['service_name']
        executable = service_config['executable']
        path = service_config['path']
        working_dir = service_config.get('working_dir', '/')
        timeout_seconds = service_config.get('timeout_minutes', 30) * 60

        start_time = datetime.now()
        logger.info(f"Starting service '{service_name}'")

        stdout_file = None
        stderr_file = None
        process = None

        try:
            # Create temporary files for stdout/stderr with sanitized names
            safe_name = sanitize_for_tempfile(service_name)
            stdout_file = tempfile.NamedTemporaryFile(
                mode='w+b',
                delete=False,
                suffix=f'_{safe_name}_stdout',
                prefix='scheduler_'
            )
            stderr_file = tempfile.NamedTemporaryFile(
                mode='w+b',
                delete=False,
                suffix=f'_{safe_name}_stderr',
                prefix='scheduler_'
            )

            # Start the subprocess
            process = subprocess.Popen(
                [executable, path],
                cwd=working_dir,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True  # Create new process group
            )

            # Update state with PID
            self.state_manager.transition_to_running(service_name, pid=process.pid)

            logger.info(f"Service '{service_name}' started with PID {process.pid}")

            # Wait for completion with timeout
            try:
                process.wait(timeout=timeout_seconds)

                # Close temp files before reading
                stdout_file.close()
                stderr_file.close()

                # Read output
                with open(stdout_file.name, 'rb') as f:
                    stdout_data = f.read()
                with open(stderr_file.name, 'rb') as f:
                    stderr_data = f.read()

                output = (
                    stdout_data.decode('utf-8', errors='replace').strip() +
                    '\n' +
                    stderr_data.decode('utf-8', errors='replace').strip()
                )

                execution_time = (datetime.now() - start_time).total_seconds()

                if process.returncode == 0:
                    self.state_manager.mark_completed(service_name, exit_code=0)
                    self.circuit_breaker.record_success(service_name)
                    logger.info(
                        f"Service '{service_name}' completed successfully "
                        f"({execution_time:.1f}s)"
                    )
                    self._log_output(service_name, output, 'completed')
                    return True
                else:
                    error_msg = f"Exit code {process.returncode}"
                    self.state_manager.mark_failed(
                        service_name,
                        error=error_msg,
                        exit_code=process.returncode
                    )
                    self.circuit_breaker.record_failure(service_name, error_msg)
                    logger.warning(
                        f"Service '{service_name}' failed with exit code "
                        f"{process.returncode} ({execution_time:.1f}s)"
                    )
                    self._log_output(service_name, output, 'failed')
                    return False

            except subprocess.TimeoutExpired:
                # Kill the process on timeout
                logger.warning(
                    f"Service '{service_name}' timed out after "
                    f"{timeout_seconds/60:.0f} minutes"
                )

                self._kill_process(process)

                # Close and read output files
                stdout_file.close()
                stderr_file.close()

                with open(stdout_file.name, 'rb') as f:
                    stdout_data = f.read()
                with open(stderr_file.name, 'rb') as f:
                    stderr_data = f.read()

                output = (
                    f"TIMEOUT: Process killed after {timeout_seconds/60:.0f} minutes\n"
                    + stdout_data.decode('utf-8', errors='replace').strip() +
                    '\n' +
                    stderr_data.decode('utf-8', errors='replace').strip()
                )

                error_msg = f"Timeout after {timeout_seconds/60:.0f} minutes"
                self.state_manager.mark_failed(
                    service_name,
                    error=error_msg,
                    exit_code=-1
                )
                self.circuit_breaker.record_failure(service_name, error_msg)
                self._log_output(service_name, output, 'timeout')
                return False

        except FileNotFoundError as e:
            error_msg = f"Script not found: {path}"
            logger.error(f"Service '{service_name}' failed: {error_msg}")
            self.state_manager.mark_failed(service_name, error=error_msg)
            self.circuit_breaker.record_failure(service_name, error_msg)
            self._log_output(service_name, str(e), 'failed')
            return False

        except Exception as e:
            error_msg = str(e)
            logger.error(
                f"Service '{service_name}' failed with exception: {error_msg}",
                exc_info=True
            )
            self.state_manager.mark_failed(service_name, error=error_msg)
            self.circuit_breaker.record_failure(service_name, error_msg)
            self._log_output(service_name, error_msg, 'failed')
            return False

        finally:
            # Clean up temp files
            self._cleanup_temp_files(stdout_file, stderr_file)

    def _kill_process(self, process: subprocess.Popen):
        """Kill a process and its process group."""
        try:
            # Try SIGTERM first
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

            # Wait for graceful termination
            time.sleep(5)

            # Force kill if still running
            if process.poll() is None:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

            # Final wait to reap zombie
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

        except Exception as e:
            logger.error(f"Error killing process {process.pid}: {e}")

    def _cleanup_temp_files(self, stdout_file, stderr_file):
        """Clean up temporary files."""
        for f in [stdout_file, stderr_file]:
            if f is not None:
                try:
                    if not f.closed:
                        f.close()
                    if hasattr(f, 'name') and os.path.exists(f.name):
                        os.unlink(f.name)
                except Exception as e:
                    logger.debug(f"Error cleaning up temp file: {e}")

    def _log_output(self, service_name: str, output: str, status: str):
        """Log service output to its dedicated log file."""
        log_filename = sanitize_for_log(service_name)
        log_file = os.path.join(self.log_dir, log_filename)

        try:
            # Rotate log if needed
            self._rotate_log_if_needed(log_file)

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(log_file, 'a') as f:
                f.write(f"[{timestamp}] Service: {service_name}\n")
                f.write(f"Status: {status}\n")
                f.write(f"Output:\n{output}\n")
                f.write("-" * 50 + "\n")

        except Exception as e:
            logger.error(f"Error writing to log file {log_file}: {e}")

    def _rotate_log_if_needed(self, log_file: str):
        """Rotate log file if it exceeds max size."""
        try:
            if os.path.exists(log_file):
                file_size = os.path.getsize(log_file)
                if file_size > self.MAX_LOG_SIZE:
                    # Keep the most recent half
                    with open(log_file, 'rb') as f:
                        f.seek(-self.MAX_LOG_SIZE // 2, os.SEEK_END)
                        recent_data = f.read()

                    with open(log_file, 'wb') as f:
                        f.write(b"[LOG ROTATED - Previous entries truncated]\n")
                        f.write(recent_data)

                    logger.info(f"Rotated log file {log_file} (was {file_size} bytes)")
        except Exception as e:
            logger.error(f"Error rotating log file {log_file}: {e}")

    def shutdown(self, wait: bool = True):
        """Shutdown the executor."""
        self._shutdown = True
        self._executor.shutdown(wait=wait)

    def get_stats(self) -> Dict[str, Any]:
        """Get executor statistics."""
        return {
            'max_workers': self._executor._max_workers,
            'active_threads': len(self._executor._threads) if hasattr(self._executor, '_threads') else 0,
        }
