#!/usr/bin/env python3
"""
Migration script to convert JSON records to SQLite database.
Run this once before starting scheduler v2.
"""

import json
import os
import sys
import shutil
from datetime import datetime

# Add parent directory to path to import lib modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.state_manager import StateManager


# Default paths
DEFAULT_JSON_PATH = '/home/gomer/servicesRecords.json'
DEFAULT_DB_PATH = '/home/gomer/pythonCron/scheduler_state.db'
DEFAULT_CONFIG_PATH = '/home/gomer/pythonCron/config.json'


def load_json_records(json_path: str) -> list:
    """Load records from JSON file."""
    if not os.path.exists(json_path):
        print(f"No existing JSON records found at {json_path}")
        return []

    try:
        with open(json_path, 'r') as f:
            records = json.load(f)
        print(f"Loaded {len(records)} records from {json_path}")
        return records
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        # Try to recover from backup
        backup_path = json_path + '.bak'
        if os.path.exists(backup_path):
            print(f"Trying backup at {backup_path}...")
            try:
                with open(backup_path, 'r') as f:
                    records = json.load(f)
                print(f"Loaded {len(records)} records from backup")
                return records
            except Exception as e2:
                print(f"Backup also failed: {e2}")
        return []
    except Exception as e:
        print(f"Error loading records: {e}")
        return []


def load_config(config_path: str) -> list:
    """Load service config to get all service names."""
    if not os.path.exists(config_path):
        print(f"Warning: Config file not found at {config_path}")
        return []

    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        print(f"Loaded {len(config)} services from config")
        return config
    except Exception as e:
        print(f"Error loading config: {e}")
        return []


def migrate(
    json_path: str = DEFAULT_JSON_PATH,
    db_path: str = DEFAULT_DB_PATH,
    config_path: str = DEFAULT_CONFIG_PATH,
    backup: bool = True
) -> bool:
    """
    Migrate JSON records to SQLite.

    Args:
        json_path: Path to existing JSON records file
        db_path: Path for new SQLite database
        config_path: Path to config.json
        backup: Whether to backup existing database if present

    Returns:
        True if migration successful
    """
    print("=" * 60)
    print("Scheduler v2 Migration: JSON -> SQLite")
    print("=" * 60)

    # Backup existing database if present
    if os.path.exists(db_path):
        if backup:
            backup_path = db_path + f'.backup.{datetime.now().strftime("%Y%m%d_%H%M%S")}'
            print(f"Backing up existing database to {backup_path}")
            shutil.copy2(db_path, backup_path)
        else:
            print("Warning: Existing database will be overwritten")

    # Initialize state manager (creates schema)
    print(f"Initializing SQLite database at {db_path}")
    state_manager = StateManager(db_path)

    # Load JSON records
    records = load_json_records(json_path)

    # Load config to get all service names
    config = load_config(config_path)
    config_service_names = {svc.get('service_name') for svc in config}

    # Migrate records
    migrated_count = 0
    skipped_count = 0

    for record in records:
        service_name = record.get('service')
        if not service_name:
            print(f"  Skipping record with no service name: {record}")
            skipped_count += 1
            continue

        status = record.get('status', 'never')
        last_executed = record.get('last_executed')

        # Handle "Never" string
        if last_executed == 'Never' or last_executed is None:
            last_executed = None

        try:
            state_manager.upsert_service(
                service_name=service_name,
                status=status,
                last_execution=last_executed
            )
            print(f"  Migrated: {service_name} (status: {status})")
            migrated_count += 1
        except Exception as e:
            print(f"  Error migrating {service_name}: {e}")
            skipped_count += 1

    # Add services from config that aren't in records
    existing_services = {svc['service_name'] for svc in state_manager.get_all_services()}
    new_services = config_service_names - existing_services

    for service_name in new_services:
        try:
            state_manager.upsert_service(
                service_name=service_name,
                status='pending'
            )
            print(f"  Added new service: {service_name}")
            migrated_count += 1
        except Exception as e:
            print(f"  Error adding {service_name}: {e}")

    # Close connection
    state_manager.close()

    # Summary
    print("\n" + "-" * 40)
    print("Migration Summary:")
    print(f"  Total records processed: {len(records)}")
    print(f"  Successfully migrated: {migrated_count}")
    print(f"  Skipped/errors: {skipped_count}")
    print(f"  New services from config: {len(new_services)}")
    print(f"  Database location: {db_path}")

    # Verify database
    print("\nVerifying database...")
    verify_state = StateManager(db_path)
    all_services = verify_state.get_all_services()
    print(f"  Total services in database: {len(all_services)}")

    # Show status summary
    status_counts = {}
    for svc in all_services:
        status = svc.get('status', 'unknown')
        status_counts[status] = status_counts.get(status, 0) + 1
    print(f"  Status summary: {status_counts}")

    verify_state.close()

    print("\n" + "=" * 60)
    print("Migration complete!")
    print("=" * 60)

    return True


def verify_database(db_path: str = DEFAULT_DB_PATH):
    """Verify the migrated database."""
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return

    print(f"\nDatabase contents ({db_path}):")
    print("-" * 60)

    state_manager = StateManager(db_path)
    services = state_manager.get_all_services()

    for svc in services:
        print(f"  {svc['service_name']}")
        print(f"    Status: {svc.get('status', 'unknown')}")
        print(f"    Last execution: {svc.get('last_execution_end', 'Never')}")
        print(f"    Failures: {svc.get('consecutive_failures', 0)}")
        print()

    state_manager.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Migrate JSON records to SQLite')
    parser.add_argument(
        '--json-path',
        default=DEFAULT_JSON_PATH,
        help=f'Path to JSON records file (default: {DEFAULT_JSON_PATH})'
    )
    parser.add_argument(
        '--db-path',
        default=DEFAULT_DB_PATH,
        help=f'Path for SQLite database (default: {DEFAULT_DB_PATH})'
    )
    parser.add_argument(
        '--config-path',
        default=DEFAULT_CONFIG_PATH,
        help=f'Path to config.json (default: {DEFAULT_CONFIG_PATH})'
    )
    parser.add_argument(
        '--no-backup',
        action='store_true',
        help='Skip backing up existing database'
    )
    parser.add_argument(
        '--verify-only',
        action='store_true',
        help='Only verify existing database, do not migrate'
    )

    args = parser.parse_args()

    if args.verify_only:
        verify_database(args.db_path)
    else:
        success = migrate(
            json_path=args.json_path,
            db_path=args.db_path,
            config_path=args.config_path,
            backup=not args.no_backup
        )
        sys.exit(0 if success else 1)
