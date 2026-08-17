#!/usr/bin/env python3
"""
Unit tests for scheduler v2 components.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.state_manager import StateManager
from lib.circuit_breaker import CircuitBreaker
from lib.health_monitor import HealthMonitor
from lib.service_executor import ServiceExecutor, sanitize_for_tempfile, sanitize_for_log
from lib.config_validator import ConfigValidator, ConfigValidationError


class TestStateManager(unittest.TestCase):
    """Tests for StateManager class."""

    def setUp(self):
        """Create a temporary database for each test."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.state_manager = StateManager(self.temp_db.name)

    def tearDown(self):
        """Clean up temporary database."""
        self.state_manager.close()
        try:
            os.unlink(self.temp_db.name)
            # Also clean up WAL files
            for suffix in ['-wal', '-shm']:
                try:
                    os.unlink(self.temp_db.name + suffix)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass

    def test_get_service_status_new_service(self):
        """Test getting status of a service that doesn't exist."""
        status = self.state_manager.get_service_status('nonexistent')
        self.assertIsNone(status)

    def test_transition_to_running(self):
        """Test atomic transition to running state."""
        # First transition should succeed
        result = self.state_manager.transition_to_running('test_service', pid=12345)
        self.assertTrue(result)

        # Second transition should fail (already running)
        result = self.state_manager.transition_to_running('test_service', pid=12346)
        self.assertFalse(result)

        # Verify status
        status = self.state_manager.get_service_status('test_service')
        self.assertEqual(status['status'], 'running')
        self.assertEqual(status['pid'], 12345)

    def test_mark_completed(self):
        """Test marking a service as completed."""
        self.state_manager.transition_to_running('test_service')
        self.state_manager.mark_completed('test_service', exit_code=0)

        status = self.state_manager.get_service_status('test_service')
        self.assertEqual(status['status'], 'completed')
        self.assertEqual(status['consecutive_failures'], 0)

    def test_mark_failed_increments_failures(self):
        """Test that marking failed increments failure count."""
        self.state_manager.transition_to_running('test_service')
        self.state_manager.mark_failed('test_service', error='Error 1')

        status = self.state_manager.get_service_status('test_service')
        self.assertEqual(status['status'], 'failed')
        self.assertEqual(status['consecutive_failures'], 1)

        # Fail again
        self.state_manager.transition_to_running('test_service')
        self.state_manager.mark_failed('test_service', error='Error 2')

        status = self.state_manager.get_service_status('test_service')
        self.assertEqual(status['consecutive_failures'], 2)

    def test_get_stuck_services(self):
        """Test detecting stuck services."""
        # Create a service that started 4 hours ago
        self.state_manager.transition_to_running('stuck_service')

        # Manually backdate the start time
        with self.state_manager._get_cursor(write=True) as cursor:
            old_time = (datetime.now() - timedelta(hours=4)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute(
                'UPDATE service_records SET last_execution_start = ? WHERE service_name = ?',
                (old_time, 'stuck_service')
            )

        stuck = self.state_manager.get_stuck_services(timedelta(hours=3))
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0]['service_name'], 'stuck_service')

    def test_concurrent_transitions(self):
        """Test thread safety of transitions."""
        successful_transitions = []

        def try_transition(service_name):
            result = self.state_manager.transition_to_running(service_name)
            if result:
                successful_transitions.append(service_name)

        # Try to transition the same service from multiple threads
        threads = [
            threading.Thread(target=try_transition, args=('concurrent_test',))
            for _ in range(10)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Only one transition should succeed
        self.assertEqual(len(successful_transitions), 1)

    def test_sync_services(self):
        """Test syncing services with config."""
        services = ['service1', 'service2', 'service3']
        self.state_manager.sync_services(services)

        all_services = self.state_manager.get_all_services()
        self.assertEqual(len(all_services), 3)

    def test_upsert_service(self):
        """Test upsert operation."""
        self.state_manager.upsert_service('test', status='completed', last_execution='2024-01-01 12:00:00')
        status = self.state_manager.get_service_status('test')
        self.assertEqual(status['status'], 'completed')

        # Update
        self.state_manager.upsert_service('test', status='failed')
        status = self.state_manager.get_service_status('test')
        self.assertEqual(status['status'], 'failed')


class TestCircuitBreaker(unittest.TestCase):
    """Tests for CircuitBreaker class."""

    def setUp(self):
        """Create temporary database and circuit breaker."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.state_manager = StateManager(self.temp_db.name)
        self.circuit_breaker = CircuitBreaker(self.state_manager)

    def tearDown(self):
        """Clean up."""
        self.state_manager.close()
        try:
            os.unlink(self.temp_db.name)
            for suffix in ['-wal', '-shm']:
                try:
                    os.unlink(self.temp_db.name + suffix)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass

    def test_circuit_starts_closed(self):
        """Test that circuit starts in closed state."""
        self.assertFalse(self.circuit_breaker.is_open('new_service'))
        self.assertTrue(self.circuit_breaker.can_execute('new_service'))

    def test_circuit_opens_after_threshold_failures(self):
        """Test that circuit opens after N failures."""
        # Record failures
        self.state_manager.upsert_service('failing_service', status='pending')

        for i in range(3):
            self.state_manager.transition_to_running('failing_service')
            self.state_manager.mark_failed('failing_service', error=f'Error {i}')
            self.circuit_breaker.record_failure('failing_service', f'Error {i}')

        # Circuit should be open now
        self.assertTrue(self.circuit_breaker.is_open('failing_service'))
        self.assertFalse(self.circuit_breaker.can_execute('failing_service'))

    def test_circuit_closes_on_success(self):
        """Test that circuit closes on successful execution."""
        # Open the circuit
        self.state_manager.upsert_service('test_service', status='pending')
        for i in range(3):
            self.state_manager.transition_to_running('test_service')
            self.state_manager.mark_failed('test_service')
            self.circuit_breaker.record_failure('test_service')

        # Record success
        self.circuit_breaker.record_success('test_service')

        # Circuit should be closed
        self.assertFalse(self.circuit_breaker.is_open('test_service'))

    def test_force_close(self):
        """Test force closing a circuit."""
        self.state_manager.upsert_service('test_service', status='circuit_open')
        self.state_manager.open_circuit('test_service', 3600)

        self.circuit_breaker.force_close('test_service')
        self.assertFalse(self.circuit_breaker.is_open('test_service'))

    def test_get_state(self):
        """Test getting circuit breaker state."""
        state = self.circuit_breaker.get_state('new_service')
        self.assertEqual(state['state'], 'CLOSED')
        self.assertEqual(state['failures'], 0)
        self.assertTrue(state['can_execute'])


class TestServiceExecutor(unittest.TestCase):
    """Tests for ServiceExecutor class."""

    def test_sanitize_for_tempfile(self):
        """Test service name sanitization for tempfiles."""
        # Test problematic names from the plan
        self.assertEqual(
            sanitize_for_tempfile('Backup Zin EAF/SRT files'),
            'Backup_Zin_EAF_SRT_files'
        )
        self.assertEqual(
            sanitize_for_tempfile('match Vicon FBX/CSV files'),
            'match_Vicon_FBX_CSV_files'
        )
        self.assertEqual(
            sanitize_for_tempfile('Test:Service*Name?'),
            'Test_Service_Name_'
        )
        self.assertEqual(
            sanitize_for_tempfile('Simple Name'),
            'Simple_Name'
        )

    def test_sanitize_for_log(self):
        """Test service name sanitization for log files."""
        self.assertEqual(sanitize_for_log('Test Service'), 'Test_Service.log')
        self.assertEqual(sanitize_for_log('Service/With/Slashes'), 'ServiceWithSlashes.log')
        self.assertEqual(sanitize_for_log('Service:Name'), 'ServiceName.log')


class TestConfigValidator(unittest.TestCase):
    """Tests for ConfigValidator class."""

    def test_valid_config(self):
        """Test validation of a valid config."""
        config = [
            {
                "service_name": "Test Service",
                "executable": "/usr/bin/python3",
                "path": __file__,  # Use this file as a valid path
                "working_dir": os.path.dirname(__file__),
                "interval_minutes": 60,
                "time_or_minute": "minute"
            }
        ]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            import json
            json.dump(config, f)
            f.flush()
            config_path = f.name

        try:
            validator = ConfigValidator(config_path)
            is_valid, errors, warnings = validator.validate()
            self.assertTrue(is_valid)
            self.assertEqual(len(errors), 0)
        finally:
            os.unlink(config_path)

    def test_missing_required_fields(self):
        """Test validation catches missing required fields."""
        config = [
            {
                "service_name": "Incomplete Service"
                # Missing other required fields
            }
        ]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            import json
            json.dump(config, f)
            f.flush()
            config_path = f.name

        try:
            validator = ConfigValidator(config_path)
            is_valid, errors, warnings = validator.validate()
            self.assertFalse(is_valid)
            self.assertGreater(len(errors), 0)
        finally:
            os.unlink(config_path)

    def test_duplicate_service_names(self):
        """Test validation catches duplicate service names."""
        config = [
            {
                "service_name": "Duplicate",
                "executable": "/usr/bin/python3",
                "path": __file__,
                "working_dir": os.path.dirname(__file__),
                "interval_minutes": 60,
                "time_or_minute": "minute"
            },
            {
                "service_name": "Duplicate",
                "executable": "/usr/bin/python3",
                "path": __file__,
                "working_dir": os.path.dirname(__file__),
                "interval_minutes": 30,
                "time_or_minute": "minute"
            }
        ]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            import json
            json.dump(config, f)
            f.flush()
            config_path = f.name

        try:
            validator = ConfigValidator(config_path)
            is_valid, errors, warnings = validator.validate()
            self.assertFalse(is_valid)
            self.assertTrue(any('Duplicate' in e for e in errors))
        finally:
            os.unlink(config_path)

    def test_invalid_time_format(self):
        """Test validation catches invalid scheduled_time format."""
        config = [
            {
                "service_name": "Timed Service",
                "executable": "/usr/bin/python3",
                "path": __file__,
                "working_dir": os.path.dirname(__file__),
                "interval_minutes": 1440,
                "time_or_minute": "time",
                "scheduled_time": "invalid"
            }
        ]

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            import json
            json.dump(config, f)
            f.flush()
            config_path = f.name

        try:
            validator = ConfigValidator(config_path)
            is_valid, errors, warnings = validator.validate()
            self.assertFalse(is_valid)
            self.assertTrue(any('HH:MM' in e for e in errors))
        finally:
            os.unlink(config_path)


class TestHealthMonitor(unittest.TestCase):
    """Tests for HealthMonitor class."""

    def setUp(self):
        """Create temporary database and health monitor."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.watchdog_log = tempfile.NamedTemporaryFile(delete=False, suffix='.log')
        self.watchdog_log.close()

        self.state_manager = StateManager(self.temp_db.name)
        self.health_monitor = HealthMonitor(
            self.state_manager,
            self.watchdog_log.name
        )

    def tearDown(self):
        """Clean up."""
        self.health_monitor.stop()
        self.state_manager.close()
        try:
            os.unlink(self.temp_db.name)
            os.unlink(self.watchdog_log.name)
            for suffix in ['-wal', '-shm']:
                try:
                    os.unlink(self.temp_db.name + suffix)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass

    def test_heartbeat_update(self):
        """Test heartbeat updates."""
        initial_heartbeat = self.health_monitor._last_heartbeat
        time.sleep(0.1)
        self.health_monitor.update_heartbeat()
        self.assertGreater(
            self.health_monitor._last_heartbeat,
            initial_heartbeat
        )

    def test_check_main_loop_alive(self):
        """Test main loop alive check."""
        self.health_monitor.update_heartbeat()
        self.assertTrue(self.health_monitor.check_main_loop_alive())

    def test_get_health_status(self):
        """Test health status retrieval."""
        status = self.health_monitor.get_health_status()
        self.assertIn('healthy', status)
        self.assertIn('main_loop_alive', status)
        self.assertIn('memory_mb', status)
        self.assertIn('status_counts', status)


class TestIntegration(unittest.TestCase):
    """Integration tests for scheduler components."""

    def setUp(self):
        """Set up integration test environment."""
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
        self.temp_db.close()
        self.temp_log_dir = tempfile.mkdtemp()

        self.state_manager = StateManager(self.temp_db.name)
        self.circuit_breaker = CircuitBreaker(self.state_manager)
        self.executor = ServiceExecutor(
            self.state_manager,
            self.circuit_breaker,
            log_dir=self.temp_log_dir,
            max_workers=5
        )

    def tearDown(self):
        """Clean up integration test environment."""
        self.executor.shutdown()
        self.state_manager.close()
        try:
            os.unlink(self.temp_db.name)
            for suffix in ['-wal', '-shm']:
                try:
                    os.unlink(self.temp_db.name + suffix)
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass

        # Clean up temp log dir
        import shutil
        try:
            shutil.rmtree(self.temp_log_dir)
        except:
            pass

    def test_full_execution_cycle(self):
        """Test complete service execution cycle."""
        # Create a simple test script
        test_script = tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.py',
            delete=False
        )
        test_script.write('print("Hello from test")\n')
        test_script.close()

        try:
            service_config = {
                'service_name': 'Integration Test Service',
                'executable': sys.executable,
                'path': test_script.name,
                'working_dir': os.path.dirname(test_script.name),
                'timeout_minutes': 1
            }

            # Execute synchronously
            result = self.executor.execute_service_sync(service_config)
            self.assertTrue(result)

            # Check state
            status = self.state_manager.get_service_status('Integration Test Service')
            self.assertEqual(status['status'], 'completed')
            self.assertEqual(status['consecutive_failures'], 0)

        finally:
            os.unlink(test_script.name)

    def test_failing_service_triggers_circuit_breaker(self):
        """Test that repeated failures trigger circuit breaker."""
        # Create a script that always fails
        test_script = tempfile.NamedTemporaryFile(
            mode='w',
            suffix='.py',
            delete=False
        )
        test_script.write('import sys; sys.exit(1)\n')
        test_script.close()

        try:
            service_config = {
                'service_name': 'Failing Service',
                'executable': sys.executable,
                'path': test_script.name,
                'working_dir': os.path.dirname(test_script.name),
                'timeout_minutes': 1
            }

            # Execute multiple times
            for i in range(4):
                # Reset status to allow re-execution
                if i > 0:
                    with self.state_manager._get_cursor(write=True) as cursor:
                        cursor.execute(
                            "UPDATE service_records SET status = 'pending' WHERE service_name = ?",
                            ('Failing Service',)
                        )

                result = self.executor.execute_service_sync(service_config)
                self.assertFalse(result)

            # Circuit breaker should be open now
            self.assertTrue(self.circuit_breaker.is_open('Failing Service'))

        finally:
            os.unlink(test_script.name)


if __name__ == '__main__':
    unittest.main(verbosity=2)
