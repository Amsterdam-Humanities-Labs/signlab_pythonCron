"""
Configuration validator for scheduler v2.
Validates service configurations before running.
"""

import json
import os
import logging
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class ConfigValidationError(Exception):
    """Raised when configuration validation fails."""
    pass


class ConfigValidator:
    """Validates service configurations."""

    # Required fields for each service
    REQUIRED_FIELDS = [
        'service_name',
        'executable',
        'path',
        'working_dir',
        'interval_minutes',
        'time_or_minute',
    ]

    # Valid values for time_or_minute
    VALID_TIME_TYPES = ['minute', 'time']

    def __init__(self, config_path: str):
        """
        Initialize the config validator.

        Args:
            config_path: Path to the config.json file
        """
        self.config_path = config_path
        self._config = None
        self._errors = []
        self._warnings = []

    def load_config(self) -> List[Dict[str, Any]]:
        """
        Load and parse the configuration file.

        Returns:
            List of service configurations

        Raises:
            ConfigValidationError: If config file cannot be loaded
        """
        try:
            with open(self.config_path, 'r') as f:
                self._config = json.load(f)

            if not isinstance(self._config, list):
                raise ConfigValidationError(
                    f"Config must be a list, got {type(self._config).__name__}"
                )

            return self._config

        except json.JSONDecodeError as e:
            raise ConfigValidationError(f"Invalid JSON in config file: {e}")
        except FileNotFoundError:
            raise ConfigValidationError(f"Config file not found: {self.config_path}")
        except Exception as e:
            raise ConfigValidationError(f"Error loading config: {e}")

    def validate(self) -> Tuple[bool, List[str], List[str]]:
        """
        Validate the entire configuration.

        Returns:
            Tuple of (is_valid, errors, warnings)
        """
        self._errors = []
        self._warnings = []

        if self._config is None:
            self.load_config()

        seen_names = set()

        for i, service in enumerate(self._config):
            service_name = service.get('service_name', f'<index {i}>')

            # Check for duplicate names
            if service_name in seen_names:
                self._errors.append(
                    f"Duplicate service name: '{service_name}'"
                )
            seen_names.add(service_name)

            # Validate this service
            self._validate_service(service, i)

        is_valid = len(self._errors) == 0
        return is_valid, self._errors, self._warnings

    def _validate_service(self, service: Dict[str, Any], index: int):
        """Validate a single service configuration."""
        service_name = service.get('service_name', f'<index {index}>')

        # Check required fields
        for field in self.REQUIRED_FIELDS:
            if field not in service:
                self._errors.append(
                    f"Service '{service_name}': missing required field '{field}'"
                )

        # Validate service_name
        if 'service_name' in service:
            name = service['service_name']
            if not name or not isinstance(name, str):
                self._errors.append(
                    f"Service at index {index}: service_name must be a non-empty string"
                )

        # Validate executable
        if 'executable' in service:
            executable = service['executable']
            if not os.path.exists(executable):
                self._warnings.append(
                    f"Service '{service_name}': executable not found: {executable}"
                )
            elif not os.access(executable, os.X_OK):
                self._warnings.append(
                    f"Service '{service_name}': executable not executable: {executable}"
                )

        # Validate script path
        if 'path' in service:
            path = service['path']
            if not os.path.exists(path):
                self._errors.append(
                    f"Service '{service_name}': script not found: {path}"
                )
            elif not os.access(path, os.R_OK):
                self._warnings.append(
                    f"Service '{service_name}': script not readable: {path}"
                )

        # Validate working directory
        if 'working_dir' in service:
            working_dir = service['working_dir']
            if not os.path.isdir(working_dir):
                self._errors.append(
                    f"Service '{service_name}': working_dir is not a directory: {working_dir}"
                )

        # Validate interval_minutes
        if 'interval_minutes' in service:
            interval = service['interval_minutes']
            if not isinstance(interval, (int, float)) or interval <= 0:
                self._errors.append(
                    f"Service '{service_name}': interval_minutes must be a positive number"
                )

        # Validate time_or_minute
        if 'time_or_minute' in service:
            time_type = service['time_or_minute']
            if time_type not in self.VALID_TIME_TYPES:
                self._errors.append(
                    f"Service '{service_name}': time_or_minute must be one of "
                    f"{self.VALID_TIME_TYPES}, got '{time_type}'"
                )

        # Validate scheduled_time for 'time' type services
        time_or_minute = service.get('time_or_minute')
        scheduled_time = service.get('scheduled_time')

        if time_or_minute == 'time':
            if not scheduled_time:
                self._errors.append(
                    f"Service '{service_name}': scheduled_time required for time_or_minute='time'"
                )
            else:
                try:
                    datetime.strptime(scheduled_time, "%H:%M")
                except ValueError:
                    self._errors.append(
                        f"Service '{service_name}': scheduled_time must be in HH:MM format, "
                        f"got '{scheduled_time}'"
                    )

        # Validate timeout_minutes if present
        if 'timeout_minutes' in service:
            timeout = service['timeout_minutes']
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                self._errors.append(
                    f"Service '{service_name}': timeout_minutes must be a positive number"
                )

    def get_valid_services(self) -> List[Dict[str, Any]]:
        """
        Get list of services that passed validation.

        Returns:
            List of valid service configurations
        """
        if self._config is None:
            self.load_config()

        valid_services = []
        seen_names = set()

        for service in self._config:
            service_name = service.get('service_name')

            # Skip duplicates
            if service_name in seen_names:
                continue
            seen_names.add(service_name)

            # Check critical fields exist
            has_required = all(
                field in service for field in self.REQUIRED_FIELDS
            )

            # Check script exists (critical error)
            script_exists = os.path.exists(service.get('path', ''))

            if has_required and script_exists:
                valid_services.append(service)
            else:
                if not script_exists:
                    logger.warning(
                        f"Skipping service '{service_name}': script not found"
                    )
                else:
                    logger.warning(
                        f"Skipping service '{service_name}': missing required fields"
                    )

        return valid_services

    def print_validation_report(self):
        """Print a human-readable validation report."""
        is_valid, errors, warnings = self.validate()

        print(f"\n{'=' * 60}")
        print("Configuration Validation Report")
        print(f"{'=' * 60}")
        print(f"Config file: {self.config_path}")
        print(f"Total services: {len(self._config)}")
        print(f"Status: {'VALID' if is_valid else 'INVALID'}")

        if errors:
            print(f"\n{'-' * 40}")
            print(f"ERRORS ({len(errors)}):")
            for error in errors:
                print(f"  - {error}")

        if warnings:
            print(f"\n{'-' * 40}")
            print(f"WARNINGS ({len(warnings)}):")
            for warning in warnings:
                print(f"  - {warning}")

        print(f"\n{'=' * 60}")

        return is_valid


def validate_config(config_path: str) -> bool:
    """
    Convenience function to validate a config file.

    Args:
        config_path: Path to the config.json file

    Returns:
        True if valid, False otherwise
    """
    validator = ConfigValidator(config_path)
    is_valid, errors, warnings = validator.validate()

    if errors:
        for error in errors:
            logger.error(f"Config error: {error}")

    if warnings:
        for warning in warnings:
            logger.warning(f"Config warning: {warning}")

    return is_valid
