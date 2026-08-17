#!/usr/bin/env python3
"""
Emergency Log Cleanup Script

This script addresses the disk space crisis by rotating large log files.
It truncates logs >100MB to keep only the most recent 50MB.

Usage:
    python3 emergency_log_cleanup.py [--dry-run] [--threshold-mb MB]
"""

import os
import sys
import argparse
from datetime import datetime

def get_file_size_mb(filepath):
    """Get file size in megabytes"""
    return os.path.getsize(filepath) / (1024 * 1024)

def rotate_large_log(filepath, max_size_mb=50, dry_run=False):
    """Rotate a large log file by keeping only the most recent data"""
    try:
        file_size_mb = get_file_size_mb(filepath)

        if dry_run:
            print(f"[DRY RUN] Would rotate {filepath} ({file_size_mb:.1f}MB)")
            return file_size_mb

        print(f"Rotating {filepath} ({file_size_mb:.1f}MB)...")

        # Read the last max_size_mb of data
        max_bytes = max_size_mb * 1024 * 1024

        with open(filepath, 'rb') as f:
            # Seek to position that will give us the last max_bytes
            file_size = os.path.getsize(filepath)
            if file_size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            recent_data = f.read()

        # Write back with header
        with open(filepath, 'wb') as f:
            header = f"[LOG ROTATED by emergency_log_cleanup.py on {datetime.now()}]\n".encode('utf-8')
            header += f"[Previous size: {file_size_mb:.1f}MB, kept most recent {max_size_mb}MB]\n\n".encode('utf-8')
            f.write(header)
            f.write(recent_data)

        new_size_mb = get_file_size_mb(filepath)
        space_saved_mb = file_size_mb - new_size_mb

        print(f"  ✓ Rotated successfully")
        print(f"  Old size: {file_size_mb:.1f}MB")
        print(f"  New size: {new_size_mb:.1f}MB")
        print(f"  Space saved: {space_saved_mb:.1f}MB")

        return space_saved_mb

    except Exception as e:
        print(f"  ✗ Error rotating {filepath}: {e}")
        return 0

def scan_and_rotate_logs(log_dir='/home/gomer/pythonCron', threshold_mb=100,
                         max_size_mb=50, dry_run=False):
    """Scan for large log files and rotate them"""
    print(f"Scanning {log_dir} for log files >{threshold_mb}MB...")
    print()

    large_logs = []

    # Find all .log files
    for filename in os.listdir(log_dir):
        if filename.endswith('.log'):
            filepath = os.path.join(log_dir, filename)

            if os.path.isfile(filepath):
                size_mb = get_file_size_mb(filepath)

                if size_mb > threshold_mb:
                    large_logs.append((filepath, size_mb))

    if not large_logs:
        print(f"No log files larger than {threshold_mb}MB found.")
        return 0

    # Sort by size (largest first)
    large_logs.sort(key=lambda x: x[1], reverse=True)

    print(f"Found {len(large_logs)} large log files:")
    print()

    for filepath, size_mb in large_logs:
        print(f"  {os.path.basename(filepath)}: {size_mb:.1f}MB")

    print()
    print("=" * 80)

    if dry_run:
        print("DRY RUN MODE - No files will be modified")
    else:
        print("Starting rotation...")

    print("=" * 80)
    print()

    total_saved = 0

    for filepath, size_mb in large_logs:
        space_saved = rotate_large_log(filepath, max_size_mb=max_size_mb, dry_run=dry_run)
        total_saved += space_saved
        print()

    print("=" * 80)
    print(f"Total space saved: {total_saved:.1f}MB ({total_saved/1024:.2f}GB)")
    print("=" * 80)

    return total_saved

def main():
    parser = argparse.ArgumentParser(description='Emergency log cleanup script')
    parser.add_argument('--log-dir', type=str, default='/home/gomer/pythonCron',
                       help='Directory containing log files')
    parser.add_argument('--threshold-mb', type=int, default=100,
                       help='Minimum file size to rotate (MB, default: 100)')
    parser.add_argument('--keep-mb', type=int, default=50,
                       help='Amount of recent data to keep (MB, default: 50)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be done without making changes')

    args = parser.parse_args()

    print()
    print("=" * 80)
    print("EMERGENCY LOG CLEANUP")
    print("=" * 80)
    print()

    total_saved = scan_and_rotate_logs(
        log_dir=args.log_dir,
        threshold_mb=args.threshold_mb,
        max_size_mb=args.keep_mb,
        dry_run=args.dry_run
    )

    if args.dry_run:
        print()
        print("This was a dry run. To actually rotate the logs, run without --dry-run:")
        print(f"  python3 emergency_log_cleanup.py")

    return 0


if __name__ == '__main__':
    sys.exit(main())
