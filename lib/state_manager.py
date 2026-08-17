"""
Thread-safe SQLite state manager for scheduler v2.
Provides ACID-compliant state management with proper locking.
"""

import sqlite3
import threading
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from contextlib import contextmanager


class StateManager:
    """Thread-safe SQLite state manager with fresh reads and atomic transitions."""

    # Valid status values
    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_COMPLETED = 'completed'
    STATUS_FAILED = 'failed'
    STATUS_CIRCUIT_OPEN = 'circuit_open'

    def __init__(self, db_path: str):
        """
        Initialize the state manager.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        self._write_lock = threading.Lock()
        self._local = threading.local()
        self._initialized = False

        # Initialize database schema
        self._initialize_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a thread-local database connection."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                isolation_level='IMMEDIATE'
            )
            self._local.connection.row_factory = sqlite3.Row
            # Enable WAL mode for better concurrent access
            self._local.connection.execute('PRAGMA journal_mode=WAL')
            self._local.connection.execute('PRAGMA synchronous=NORMAL')
        return self._local.connection

    @contextmanager
    def _get_cursor(self, write: bool = False):
        """Context manager for database cursor with optional write lock."""
        if write:
            with self._write_lock:
                conn = self._get_connection()
                cursor = conn.cursor()
                try:
                    yield cursor
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        else:
            conn = self._get_connection()
            cursor = conn.cursor()
            yield cursor

    def _initialize_db(self):
        """Initialize the database schema if not exists."""
        with self._get_cursor(write=True) as cursor:
            # Main service records table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS service_records (
                    service_name TEXT PRIMARY KEY,
                    status TEXT CHECK(status IN ('pending', 'running', 'completed', 'failed', 'circuit_open')),
                    last_execution_start DATETIME,
                    last_execution_end DATETIME,
                    pid INTEGER,
                    consecutive_failures INTEGER DEFAULT 0,
                    circuit_open_until DATETIME,
                    last_error TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Execution history for debugging and audit
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS execution_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_name TEXT NOT NULL,
                    started_at DATETIME,
                    ended_at DATETIME,
                    status TEXT,
                    exit_code INTEGER,
                    error_message TEXT,
                    duration_seconds REAL,
                    FOREIGN KEY (service_name) REFERENCES service_records(service_name)
                )
            ''')

            # Index for common queries
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_history_service
                ON execution_history(service_name, started_at DESC)
            ''')

            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_records_status
                ON service_records(status)
            ''')

        self._initialized = True

    def get_service_status(self, service_name: str) -> Optional[Dict[str, Any]]:
        """
        Get current status of a service. Fresh read - no caching.

        Args:
            service_name: Name of the service

        Returns:
            Dict with service status info, or None if not found
        """
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT service_name, status, last_execution_start, last_execution_end,
                       pid, consecutive_failures, circuit_open_until, last_error, updated_at
                FROM service_records
                WHERE service_name = ?
            ''', (service_name,))
            row = cursor.fetchone()

            if row is None:
                return None

            return dict(row)

    def get_all_services(self) -> List[Dict[str, Any]]:
        """Get all service records. Fresh read."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT service_name, status, last_execution_start, last_execution_end,
                       pid, consecutive_failures, circuit_open_until, last_error, updated_at
                FROM service_records
                ORDER BY service_name
            ''')
            return [dict(row) for row in cursor.fetchall()]

    def get_services_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Get all services with a specific status."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT service_name, status, last_execution_start, last_execution_end,
                       pid, consecutive_failures, circuit_open_until, last_error, updated_at
                FROM service_records
                WHERE status = ?
                ORDER BY service_name
            ''', (status,))
            return [dict(row) for row in cursor.fetchall()]

    def transition_to_running(self, service_name: str, pid: int = None) -> bool:
        """
        Atomically transition service to running state.
        Only succeeds if service is not already running.

        Args:
            service_name: Name of the service
            pid: Process ID of the running service

        Returns:
            True if transition succeeded, False if already running
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            # Check current status
            cursor.execute('''
                SELECT status FROM service_records WHERE service_name = ?
            ''', (service_name,))
            row = cursor.fetchone()

            if row and row['status'] == self.STATUS_RUNNING:
                return False  # Already running

            if row:
                # Update existing record
                cursor.execute('''
                    UPDATE service_records
                    SET status = ?, last_execution_start = ?, pid = ?, updated_at = ?
                    WHERE service_name = ? AND status != ?
                ''', (self.STATUS_RUNNING, now, pid, now, service_name, self.STATUS_RUNNING))
            else:
                # Insert new record
                cursor.execute('''
                    INSERT INTO service_records (service_name, status, last_execution_start, pid, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (service_name, self.STATUS_RUNNING, now, pid, now))

            return cursor.rowcount > 0

    def mark_completed(self, service_name: str, exit_code: int = 0):
        """
        Mark service as completed and reset failure count.

        Args:
            service_name: Name of the service
            exit_code: Exit code from the process
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            # Get start time for duration calculation
            cursor.execute('''
                SELECT last_execution_start FROM service_records WHERE service_name = ?
            ''', (service_name,))
            row = cursor.fetchone()
            start_time = row['last_execution_start'] if row else now

            # Calculate duration
            duration = None
            if start_time:
                try:
                    start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
                    end_dt = datetime.strptime(now, '%Y-%m-%d %H:%M:%S')
                    duration = (end_dt - start_dt).total_seconds()
                except ValueError:
                    pass

            # Update service record
            cursor.execute('''
                UPDATE service_records
                SET status = ?, last_execution_end = ?, pid = NULL,
                    consecutive_failures = 0, circuit_open_until = NULL,
                    last_error = NULL, updated_at = ?
                WHERE service_name = ?
            ''', (self.STATUS_COMPLETED, now, now, service_name))

            # Log to history
            cursor.execute('''
                INSERT INTO execution_history
                (service_name, started_at, ended_at, status, exit_code, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (service_name, start_time, now, self.STATUS_COMPLETED, exit_code, duration))

    def mark_failed(self, service_name: str, error: str = None, exit_code: int = None):
        """
        Mark service as failed and increment failure count.

        Args:
            service_name: Name of the service
            error: Error message
            exit_code: Exit code from the process
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            # Get current failure count and start time
            cursor.execute('''
                SELECT consecutive_failures, last_execution_start
                FROM service_records WHERE service_name = ?
            ''', (service_name,))
            row = cursor.fetchone()

            failures = (row['consecutive_failures'] or 0) + 1 if row else 1
            start_time = row['last_execution_start'] if row else now

            # Calculate duration
            duration = None
            if start_time:
                try:
                    start_dt = datetime.strptime(start_time, '%Y-%m-%d %H:%M:%S')
                    end_dt = datetime.strptime(now, '%Y-%m-%d %H:%M:%S')
                    duration = (end_dt - start_dt).total_seconds()
                except ValueError:
                    pass

            # Update service record
            cursor.execute('''
                UPDATE service_records
                SET status = ?, last_execution_end = ?, pid = NULL,
                    consecutive_failures = ?, last_error = ?, updated_at = ?
                WHERE service_name = ?
            ''', (self.STATUS_FAILED, now, failures, error, now, service_name))

            if cursor.rowcount == 0:
                # Record doesn't exist, create it
                cursor.execute('''
                    INSERT INTO service_records
                    (service_name, status, last_execution_end, consecutive_failures, last_error, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (service_name, self.STATUS_FAILED, now, failures, error, now))

            # Log to history
            cursor.execute('''
                INSERT INTO execution_history
                (service_name, started_at, ended_at, status, exit_code, error_message, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (service_name, start_time, now, self.STATUS_FAILED, exit_code, error, duration))

    def get_stuck_services(self, threshold: timedelta) -> List[Dict[str, Any]]:
        """
        Get services that have been running longer than the threshold.

        Args:
            threshold: Maximum allowed running time

        Returns:
            List of stuck service records
        """
        cutoff = (datetime.now() - threshold).strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT service_name, status, last_execution_start, pid, updated_at
                FROM service_records
                WHERE status = ? AND last_execution_start < ?
            ''', (self.STATUS_RUNNING, cutoff))
            return [dict(row) for row in cursor.fetchall()]

    def increment_failures(self, service_name: str) -> int:
        """
        Increment failure count for a service and return new count.

        Args:
            service_name: Name of the service

        Returns:
            New failure count
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            cursor.execute('''
                UPDATE service_records
                SET consecutive_failures = consecutive_failures + 1, updated_at = ?
                WHERE service_name = ?
            ''', (now, service_name))

            cursor.execute('''
                SELECT consecutive_failures FROM service_records WHERE service_name = ?
            ''', (service_name,))
            row = cursor.fetchone()
            return row['consecutive_failures'] if row else 1

    def open_circuit(self, service_name: str, backoff_seconds: int):
        """
        Open the circuit breaker for a service.

        Args:
            service_name: Name of the service
            backoff_seconds: How long to keep circuit open
        """
        now = datetime.now()
        open_until = (now + timedelta(seconds=backoff_seconds)).strftime('%Y-%m-%d %H:%M:%S')
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            cursor.execute('''
                UPDATE service_records
                SET status = ?, circuit_open_until = ?, updated_at = ?
                WHERE service_name = ?
            ''', (self.STATUS_CIRCUIT_OPEN, open_until, now_str, service_name))

    def close_circuit(self, service_name: str):
        """Close the circuit breaker for a service."""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            cursor.execute('''
                UPDATE service_records
                SET status = ?, circuit_open_until = NULL, consecutive_failures = 0, updated_at = ?
                WHERE service_name = ? AND status = ?
            ''', (self.STATUS_PENDING, now, service_name, self.STATUS_CIRCUIT_OPEN))

    def reset_failures(self, service_name: str):
        """Reset failure count for a service."""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            cursor.execute('''
                UPDATE service_records
                SET consecutive_failures = 0, circuit_open_until = NULL, updated_at = ?
                WHERE service_name = ?
            ''', (now, service_name))

    def sync_services(self, service_names: List[str]):
        """
        Sync database with config - add new services, mark removed ones.

        Args:
            service_names: List of service names from config
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            # Get existing services
            cursor.execute('SELECT service_name FROM service_records')
            existing = {row['service_name'] for row in cursor.fetchall()}

            # Add new services
            for name in service_names:
                if name not in existing:
                    cursor.execute('''
                        INSERT INTO service_records (service_name, status, updated_at)
                        VALUES (?, ?, ?)
                    ''', (name, self.STATUS_PENDING, now))

    def upsert_service(self, service_name: str, status: str = None,
                       last_execution: str = None):
        """
        Insert or update a service record (used for migration).

        Args:
            service_name: Name of the service
            status: Current status
            last_execution: Last execution timestamp string
        """
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Map old statuses to new
        status_map = {
            'never': self.STATUS_PENDING,
            'running': self.STATUS_RUNNING,
            'completed': self.STATUS_COMPLETED,
            'failed': self.STATUS_FAILED,
        }
        mapped_status = status_map.get(status, self.STATUS_PENDING)

        with self._get_cursor(write=True) as cursor:
            cursor.execute('''
                INSERT INTO service_records (service_name, status, last_execution_end, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(service_name) DO UPDATE SET
                    status = excluded.status,
                    last_execution_end = excluded.last_execution_end,
                    updated_at = excluded.updated_at
            ''', (service_name, mapped_status, last_execution, now))

    def get_failure_count(self, service_name: str) -> int:
        """Get current failure count for a service."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT consecutive_failures FROM service_records WHERE service_name = ?
            ''', (service_name,))
            row = cursor.fetchone()
            return row['consecutive_failures'] if row else 0

    def get_circuit_open_until(self, service_name: str) -> Optional[datetime]:
        """Get the time until which the circuit is open."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT circuit_open_until FROM service_records WHERE service_name = ?
            ''', (service_name,))
            row = cursor.fetchone()
            if row and row['circuit_open_until']:
                return datetime.strptime(row['circuit_open_until'], '%Y-%m-%d %H:%M:%S')
            return None

    def get_recent_history(self, service_name: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent execution history for a service."""
        with self._get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM execution_history
                WHERE service_name = ?
                ORDER BY started_at DESC
                LIMIT ?
            ''', (service_name, limit))
            return [dict(row) for row in cursor.fetchall()]

    def cleanup_old_history(self, days: int = 30):
        """Remove execution history older than specified days."""
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')

        with self._get_cursor(write=True) as cursor:
            cursor.execute('''
                DELETE FROM execution_history WHERE started_at < ?
            ''', (cutoff,))
            return cursor.rowcount

    def close(self):
        """Close database connection for current thread."""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None
