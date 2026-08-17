#!/bin/bash
# Stop all service wrappers

echo "Stopping all service wrappers..."
pkill -f 'python3.*service_wrapper.py'
sleep 2
echo "Services stopped."
