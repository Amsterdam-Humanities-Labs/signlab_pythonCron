"""
Circuit breaker pattern implementation for scheduler v2.
Prevents retrying failing services indefinitely.
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from .state_manager import StateManager

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """
    Circuit breaker to stop executing services after N consecutive failures.

    States:
    - CLOSED: Normal operation, service will be executed
    - OPEN: Service blocked, too many failures
    - HALF_OPEN: Testing if service recovered (after backoff period)
    """

    # Number of consecutive failures before opening circuit
    FAILURE_THRESHOLD = 3

    # Initial backoff time in seconds (5 minutes)
    INITIAL_BACKOFF = 300

    # Maximum backoff time in seconds (1 hour)
    MAX_BACKOFF = 3600

    # Backoff multiplier for exponential backoff
    BACKOFF_MULTIPLIER = 2

    def __init__(self, state_manager: 'StateManager'):
        """
        Initialize the circuit breaker.

        Args:
            state_manager: StateManager instance for state persistence
        """
        self.state_manager = state_manager

    def is_open(self, service_name: str) -> bool:
        """
        Check if the circuit is open (executions blocked).

        Args:
            service_name: Name of the service

        Returns:
            True if circuit is open and service should not execute
        """
        status = self.state_manager.get_service_status(service_name)

        if status is None:
            return False  # New service, circuit is closed

        # Check if circuit is explicitly open
        if status.get('status') == self.state_manager.STATUS_CIRCUIT_OPEN:
            circuit_open_until = status.get('circuit_open_until')
            if circuit_open_until:
                try:
                    open_until = datetime.strptime(circuit_open_until, '%Y-%m-%d %H:%M:%S')
                    if datetime.now() < open_until:
                        logger.debug(
                            f"Circuit OPEN for '{service_name}' until {open_until}"
                        )
                        return True
                    else:
                        # Backoff period expired, transition to half-open (pending)
                        logger.info(
                            f"Circuit HALF-OPEN for '{service_name}' - testing recovery"
                        )
                        self.state_manager.close_circuit(service_name)
                        return False
                except ValueError:
                    pass

        return False

    def can_execute(self, service_name: str) -> bool:
        """
        Check if a service can be executed (circuit is not open).

        Args:
            service_name: Name of the service

        Returns:
            True if service can be executed
        """
        return not self.is_open(service_name)

    def record_success(self, service_name: str):
        """
        Record a successful execution, reset failure count.

        Args:
            service_name: Name of the service
        """
        self.state_manager.reset_failures(service_name)
        logger.info(f"Circuit CLOSED for '{service_name}' - execution successful")

    def record_failure(self, service_name: str, error: str = None):
        """
        Record a failed execution, potentially open the circuit.

        Args:
            service_name: Name of the service
            error: Error message from the failure
        """
        failures = self.state_manager.get_failure_count(service_name)
        failures += 1  # The state_manager will increment, we're checking what it will be

        logger.warning(
            f"Service '{service_name}' failed (consecutive failures: {failures})"
        )

        if failures >= self.FAILURE_THRESHOLD:
            # Calculate backoff with exponential increase
            # After 3 failures: 5 min, 4: 10 min, 5: 20 min, etc.
            extra_failures = failures - self.FAILURE_THRESHOLD
            backoff = min(
                self.INITIAL_BACKOFF * (self.BACKOFF_MULTIPLIER ** extra_failures),
                self.MAX_BACKOFF
            )
            backoff = int(backoff)

            self.state_manager.open_circuit(service_name, backoff)
            logger.warning(
                f"Circuit OPENED for '{service_name}' - {failures} failures, "
                f"backoff: {backoff}s ({backoff/60:.1f} min)"
            )

    def get_state(self, service_name: str) -> dict:
        """
        Get the current circuit breaker state for a service.

        Args:
            service_name: Name of the service

        Returns:
            Dict with circuit state information
        """
        status = self.state_manager.get_service_status(service_name)

        if status is None:
            return {
                'state': 'CLOSED',
                'failures': 0,
                'can_execute': True,
                'open_until': None,
            }

        failures = status.get('consecutive_failures', 0)
        circuit_open_until = status.get('circuit_open_until')
        is_circuit_open = status.get('status') == self.state_manager.STATUS_CIRCUIT_OPEN

        if is_circuit_open and circuit_open_until:
            try:
                open_until = datetime.strptime(circuit_open_until, '%Y-%m-%d %H:%M:%S')
                if datetime.now() < open_until:
                    return {
                        'state': 'OPEN',
                        'failures': failures,
                        'can_execute': False,
                        'open_until': open_until,
                        'remaining_seconds': (open_until - datetime.now()).total_seconds(),
                    }
            except ValueError:
                pass

        return {
            'state': 'CLOSED' if failures < self.FAILURE_THRESHOLD else 'HALF_OPEN',
            'failures': failures,
            'can_execute': True,
            'open_until': None,
        }

    def force_close(self, service_name: str):
        """
        Manually close the circuit (admin override).

        Args:
            service_name: Name of the service
        """
        self.state_manager.close_circuit(service_name)
        self.state_manager.reset_failures(service_name)
        logger.info(f"Circuit FORCE CLOSED for '{service_name}'")

    def force_open(self, service_name: str, duration_seconds: int = 3600):
        """
        Manually open the circuit (admin override).

        Args:
            service_name: Name of the service
            duration_seconds: How long to keep circuit open
        """
        self.state_manager.open_circuit(service_name, duration_seconds)
        logger.info(
            f"Circuit FORCE OPENED for '{service_name}' for {duration_seconds}s"
        )

    def get_all_open_circuits(self) -> list:
        """
        Get all services with open circuits.

        Returns:
            List of service names with open circuits
        """
        services = self.state_manager.get_services_by_status(
            self.state_manager.STATUS_CIRCUIT_OPEN
        )
        now = datetime.now()
        open_circuits = []

        for svc in services:
            circuit_open_until = svc.get('circuit_open_until')
            if circuit_open_until:
                try:
                    open_until = datetime.strptime(circuit_open_until, '%Y-%m-%d %H:%M:%S')
                    if now < open_until:
                        open_circuits.append({
                            'service_name': svc['service_name'],
                            'failures': svc.get('consecutive_failures', 0),
                            'open_until': open_until,
                            'remaining_seconds': (open_until - now).total_seconds(),
                        })
                except ValueError:
                    pass

        return open_circuits
