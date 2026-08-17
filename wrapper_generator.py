#!/usr/bin/env python3
"""
Wrapper Generator - Generates systemd service files for service wrappers

This script reads services_config.json and generates systemd service files
for each enabled service, making it easy to deploy and manage them.

Usage:
    python3 wrapper_generator.py [--output-dir DIR] [--dry-run]

Arguments:
    --output-dir DIR    Directory to write .service files (default: ./systemd)
    --dry-run          Show what would be generated without writing files
    --no-systemd       Skip systemd file generation, only show commands
"""

import json
import os
import sys
import argparse
from pathlib import Path

class WrapperGenerator:
    def __init__(self, config_path='/home/gomer/pythonCron/services_config.json',
                 output_dir='./systemd'):
        self.config_path = config_path
        self.output_dir = output_dir
        self.config = None

    def load_config(self):
        """Load services configuration"""
        try:
            with open(self.config_path, 'r') as f:
                self.config = json.load(f)
            return True
        except Exception as e:
            print(f"ERROR: Failed to load configuration: {e}")
            return False

    def generate_systemd_service(self, service_config):
        """Generate a systemd service file for a service"""
        service_name = service_config['name']
        description = service_config.get('description', f'Service wrapper for {service_name}')

        # Systemd service file template
        service_content = f"""[Unit]
Description={description}
After=network.target

[Service]
Type=simple
User={os.getenv('USER', 'gomer')}
WorkingDirectory=/home/gomer/pythonCron
ExecStart=/usr/bin/python3 /home/gomer/pythonCron/service_wrapper.py {service_name}
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

# Resource limits (optional, uncomment and adjust as needed)
# CPUQuota=50%
# MemoryMax=512M

[Install]
WantedBy=multi-user.target
"""
        return service_content

    def get_service_filename(self, service_name):
        """Get the systemd service filename for a service"""
        # Convert service name to systemd-friendly format
        safe_name = service_name.replace(' ', '_').replace('/', '_').lower()
        return f"service-{safe_name}.service"

    def generate_all(self, dry_run=False):
        """Generate systemd service files for all enabled services"""
        if not self.load_config():
            return False

        enabled_services = [s for s in self.config.get('services', [])
                          if s.get('enabled', True)]

        print(f"Found {len(enabled_services)} enabled services")
        print()

        if not dry_run:
            os.makedirs(self.output_dir, exist_ok=True)

        generated_files = []

        for service_config in enabled_services:
            service_name = service_config['name']
            service_filename = self.get_service_filename(service_name)
            service_path = os.path.join(self.output_dir, service_filename)

            print(f"Service: {service_name}")
            print(f"  File: {service_filename}")

            service_content = self.generate_systemd_service(service_config)

            if dry_run:
                print("  Content preview:")
                for line in service_content.split('\n')[:10]:
                    print(f"    {line}")
                print("    ...")
            else:
                with open(service_path, 'w') as f:
                    f.write(service_content)
                print(f"  ✓ Written to: {service_path}")
                generated_files.append(service_path)

            print()

        return generated_files

    def print_installation_instructions(self, generated_files):
        """Print instructions for installing the generated service files"""
        print("=" * 80)
        print("INSTALLATION INSTRUCTIONS")
        print("=" * 80)
        print()
        print("To install and enable these services:")
        print()
        print("1. Copy service files to systemd directory:")
        print(f"   sudo cp {self.output_dir}/*.service /etc/systemd/system/")
        print()
        print("2. Reload systemd daemon:")
        print("   sudo systemctl daemon-reload")
        print()
        print("3. Enable services to start on boot:")
        for filepath in generated_files:
            filename = os.path.basename(filepath)
            print(f"   sudo systemctl enable {filename}")
        print()
        print("4. Start services:")
        for filepath in generated_files:
            filename = os.path.basename(filepath)
            print(f"   sudo systemctl start {filename}")
        print()
        print("5. Check status:")
        for filepath in generated_files:
            filename = os.path.basename(filepath)
            print(f"   sudo systemctl status {filename}")
        print()
        print("=" * 80)
        print("MANAGEMENT COMMANDS")
        print("=" * 80)
        print()
        print("View logs for a service:")
        print("   sudo journalctl -u service-<name>.service -f")
        print()
        print("Stop a service:")
        print("   sudo systemctl stop service-<name>.service")
        print()
        print("Restart a service:")
        print("   sudo systemctl restart service-<name>.service")
        print()
        print("Disable a service:")
        print("   sudo systemctl disable service-<name>.service")
        print()

    def print_manual_start_instructions(self):
        """Print instructions for manually starting wrappers without systemd"""
        if not self.load_config():
            return

        enabled_services = [s for s in self.config.get('services', [])
                          if s.get('enabled', True)]

        print("=" * 80)
        print("MANUAL START INSTRUCTIONS (without systemd)")
        print("=" * 80)
        print()
        print("To start services manually:")
        print()

        for service_config in enabled_services:
            service_name = service_config['name']
            print(f"# Start {service_name}")
            print(f"nohup python3 /home/gomer/pythonCron/service_wrapper.py {service_name} > /dev/null 2>&1 &")
            print()

        print("To stop all services:")
        print("pkill -f 'python3.*service_wrapper.py'")
        print()
        print("To check which services are running:")
        print("ps aux | grep service_wrapper.py")
        print()

    def generate_start_all_script(self):
        """Generate a convenience script to start all services"""
        if not self.load_config():
            return False

        enabled_services = [s for s in self.config.get('services', [])
                          if s.get('enabled', True)]

        script_content = """#!/bin/bash
# Start all service wrappers
# Generated by wrapper_generator.py

"""
        for service_config in enabled_services:
            service_name = service_config['name']
            script_content += f'echo "Starting {service_name}..."\n'
            script_content += f'nohup python3 /home/gomer/pythonCron/service_wrapper.py {service_name} > /dev/null 2>&1 &\n'
            script_content += 'sleep 1\n\n'

        script_content += 'echo "All services started."\n'
        script_content += 'echo "Check status with: ps aux | grep service_wrapper.py"\n'

        script_path = os.path.join(self.output_dir, 'start_all_wrappers.sh')

        with open(script_path, 'w') as f:
            f.write(script_content)

        os.chmod(script_path, 0o755)

        print(f"✓ Created start script: {script_path}")
        print(f"  Run with: bash {script_path}")
        print()

        # Also create stop script
        stop_script = """#!/bin/bash
# Stop all service wrappers

echo "Stopping all service wrappers..."
pkill -f 'python3.*service_wrapper.py'
sleep 2
echo "Services stopped."
"""
        stop_script_path = os.path.join(self.output_dir, 'stop_all_wrappers.sh')

        with open(stop_script_path, 'w') as f:
            f.write(stop_script)

        os.chmod(stop_script_path, 0o755)

        print(f"✓ Created stop script: {stop_script_path}")
        print(f"  Run with: bash {stop_script_path}")
        print()

        return True


def main():
    parser = argparse.ArgumentParser(description='Generate systemd service files for wrappers')
    parser.add_argument('--output-dir', type=str, default='./systemd',
                       help='Directory to write .service files (default: ./systemd)')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be generated without writing files')
    parser.add_argument('--no-systemd', action='store_true',
                       help='Skip systemd file generation, only show manual commands')
    parser.add_argument('--config', type=str, default='/home/gomer/pythonCron/services_config.json',
                       help='Path to services configuration file')

    args = parser.parse_args()

    generator = WrapperGenerator(
        config_path=args.config,
        output_dir=args.output_dir
    )

    if args.no_systemd:
        generator.print_manual_start_instructions()
        return 0

    generated_files = generator.generate_all(dry_run=args.dry_run)

    if not args.dry_run and generated_files:
        print()
        generator.print_installation_instructions(generated_files)
        print()
        generator.generate_start_all_script()

    return 0


if __name__ == '__main__':
    sys.exit(main())
