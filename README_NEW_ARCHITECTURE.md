# New Service Wrapper Architecture

## Overview

This document describes the new distributed service management architecture that replaces the centralized `scheduler.py` system. The new design provides better reliability, isolation, and observability.

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                   services_config.json                          │
│                  (Central Configuration)                         │
└─────────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────────────────┐
        │                   │                               │
        ▼                   ▼                               ▼
┌──────────────────┐ ┌──────────────────┐  ... ┌──────────────────┐
│  Wrapper Process │ │  Wrapper Process │      │  Wrapper Process │
│  (Service 1)     │ │  (Service 2)     │      │  (Service N)     │
│                  │ │                  │      │                  │
│  ┌────────────┐  │ │  ┌────────────┐  │      │  ┌────────────┐  │
│  │ Scheduling │  │ │  │ Scheduling │  │      │  │ Scheduling │  │
│  │   Loop     │  │ │  │   Loop     │  │      │  │   Loop     │  │
│  └────────────┘  │ │  └────────────┘  │      │  └────────────┘  │
│        │         │ │        │         │      │        │         │
│        ▼         │ │        ▼         │      │        ▼         │
│  ┌────────────┐  │ │  ┌────────────┐  │      │  ┌────────────┐  │
│  │  Execute   │  │ │  │  Execute   │  │      │  │  Execute   │  │
│  │   Script   │  │ │  │   Script   │  │      │  │   Script   │  │
│  └────────────┘  │ │  └────────────┘  │      │  └────────────┘  │
│        │         │ │        │         │      │        │         │
│        ▼         │ │        ▼         │      │        ▼         │
│  ┌────────────┐  │ │  ┌────────────┐  │      │  ┌────────────┐  │
│  │   Health   │  │ │  │   Health   │  │      │  │   Health   │  │
│  │  Reporting │  │ │  │  Reporting │  │      │  │  Reporting │  │
│  └────────────┘  │ │  └────────────┘  │      │  └────────────┘  │
└──────────────────┘ └──────────────────┘      └──────────────────┘
         │                   │                          │
         └───────────────────┼──────────────────────────┘
                             ▼
                   ┌──────────────────────┐
                   │  Watchdog Daemon     │
                   │  (Monitors & Restarts│
                   │   Failed Services)   │
                   └──────────────────────┘
```

## Components

### 1. services_config.json

**Location**: `/home/gomer/pythonCron/services_config.json`

Central configuration file that defines all services and their behavior.

**Key Features**:
- Structured sections for execution, health, retry, logging, and watchdog
- Support for interval-based and time-based scheduling
- Per-service timeout, retry policies, and resource limits
- Jitter support to prevent thundering herd

**Example Service Configuration**:
```json
{
  "name": "Check_Disk",
  "enabled": true,
  "description": "Checks disk space and alerts if low",

  "execution": {
    "type": "interval",
    "interval_seconds": 3600,
    "jitter_seconds": 15
  },

  "command": {
    "executable": "/usr/bin/python3",
    "script": "/home/gomer/pythonCron/checkDisk.py",
    "working_dir": "/web/pythonCron",
    "timeout_seconds": 300
  },

  "health": {
    "max_consecutive_failures": 5,
    "restart_delay_seconds": 300,
    "alert_after_failures": 10
  },

  "retry": {
    "enabled": true,
    "max_retries": 3,
    "retry_delay_seconds": 60,
    "exponential_backoff": false
  },

  "logging": {
    "level": "INFO",
    "max_size_mb": 10,
    "max_files": 5
  },

  "watchdog": {
    "enabled": true,
    "check_if_hung": true,
    "hung_threshold_seconds": 600,
    "auto_restart": true
  }
}
```

### 2. service_wrapper.py

**Location**: `/home/gomer/pythonCron/service_wrapper.py`

Generic wrapper that runs any service with self-scheduling capability.

**Features**:
- Internal scheduling loop (interval or time-based)
- Subprocess execution with timeout handling
- Automatic retry with exponential backoff
- Health status reporting to state files
- Log rotation built-in
- Graceful shutdown on SIGTERM/SIGINT
- No subprocess pipe deadlocks (uses temp files)

**Usage**:
```bash
python3 service_wrapper.py <service_name>

# Examples:
python3 service_wrapper.py Check_Disk
python3 service_wrapper.py MySQL_Backup
```

**How It Works**:
1. Loads configuration for the specified service from `services_config.json`
2. Calculates next execution time based on interval or scheduled time
3. Sleeps until execution time
4. Executes the service script with timeout
5. Retries on failure (with backoff if configured)
6. Reports status to state file
7. Repeats forever

**State Files**:
Each wrapper maintains a state file in `/home/gomer/pythonCron/state/<service_name>.json`:
```json
{
  "service": "Check_Disk",
  "status": "idle",
  "pid": 12345,
  "started_at": "2025-11-18T10:00:00",
  "last_execution": "2025-11-18T11:00:00",
  "last_success": "2025-11-18T11:00:00",
  "consecutive_failures": 0,
  "total_executions": 24,
  "total_successes": 24,
  "total_failures": 0,
  "next_execution": "2025-11-18T12:00:00"
}
```

### 3. watchdog_daemon.py

**Location**: `/home/gomer/pythonCron/watchdog_daemon.py`

Monitors all service wrappers and takes corrective action when needed.

**Features**:
- Periodic health checks (default: every hour)
- Auto-restart crashed wrapper processes
- Detect and kill stuck processes
- Clean up zombie processes
- Disk space monitoring
- Alert on repeated failures

**Usage**:
```bash
# Run as daemon (checks every hour)
python3 watchdog_daemon.py

# Run with custom check interval
python3 watchdog_daemon.py --check-interval 1800  # 30 minutes

# Run once and exit (useful for cron)
python3 watchdog_daemon.py --once
```

**How It Works**:
1. Loads configuration to find all enabled services
2. For each service:
   - Checks if wrapper process is running
   - Reads state file to check health
   - Detects stuck processes (running >2x timeout)
   - Auto-restarts if configured
3. Cleans up zombie processes
4. Checks disk space and logs warnings
5. Sleeps until next check interval

**Logs**: `/home/gomer/pythonCron/watchdog_daemon.log`

### 4. wrapper_generator.py

**Location**: `/home/gomer/pythonCron/wrapper_generator.py`

Utility to generate systemd service files and helper scripts.

**Usage**:
```bash
# Generate systemd service files
python3 wrapper_generator.py

# Dry run (preview only)
python3 wrapper_generator.py --dry-run

# Generate start/stop scripts (without systemd)
python3 wrapper_generator.py --no-systemd
```

**Outputs**:
- `systemd/*.service` - Systemd service files for each wrapper
- `systemd/start_all_wrappers.sh` - Script to start all services manually
- `systemd/stop_all_wrappers.sh` - Script to stop all services

### 5. emergency_log_cleanup.py

**Location**: `/home/gomer/pythonCron/emergency_log_cleanup.py`

Emergency utility to rotate large log files.

**Usage**:
```bash
# Check what would be rotated
python3 emergency_log_cleanup.py --dry-run

# Rotate logs >100MB, keeping most recent 50MB
python3 emergency_log_cleanup.py

# Custom thresholds
python3 emergency_log_cleanup.py --threshold-mb 200 --keep-mb 100
```

## Migration Guide

### Phase 1: Preparation

1. **Review configuration**:
   ```bash
   cat /home/gomer/pythonCron/services_config.json
   ```

2. **Test wrapper with low-risk service**:
   ```bash
   # Terminal 1: Start wrapper
   python3 service_wrapper.py Check_Disk

   # Terminal 2: Monitor logs
   tail -f /home/gomer/pythonCron/logs/Check_Disk.log

   # Terminal 3: Check state
   watch -n 5 'cat /home/gomer/pythonCron/state/Check_Disk.json'
   ```

3. **Verify wrapper works correctly** for 30-60 minutes

### Phase 2: Pilot Deployment

**Deploy 3 low-risk services**:

```bash
# Create necessary directories
mkdir -p /home/gomer/pythonCron/logs
mkdir -p /home/gomer/pythonCron/state

# Start pilot services
nohup python3 service_wrapper.py Check_Disk > /dev/null 2>&1 &
nohup python3 service_wrapper.py Clean_lock_files > /dev/null 2>&1 &
nohup python3 service_wrapper.py Get_Themas > /dev/null 2>&1 &

# Verify they're running
ps aux | grep service_wrapper.py

# Monitor for 24-48 hours
```

### Phase 3: Full Migration

**Option A: Using systemd (Recommended)**

```bash
# Generate service files
python3 wrapper_generator.py

# Install services
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload

# Enable and start all services
for service in systemd/*.service; do
    name=$(basename $service)
    sudo systemctl enable $name
    sudo systemctl start $name
done

# Check status
systemctl list-units | grep service-
```

**Option B: Manual Start**

```bash
# Generate start script
python3 wrapper_generator.py --no-systemd

# Start all services
bash systemd/start_all_wrappers.sh

# Verify
ps aux | grep service_wrapper.py
```

### Phase 4: Enable Watchdog

```bash
# Start watchdog daemon
nohup python3 watchdog_daemon.py > /dev/null 2>&1 &

# Or with systemd:
sudo cp systemd/watchdog-daemon.service /etc/systemd/system/
sudo systemctl enable watchdog-daemon.service
sudo systemctl start watchdog-daemon.service

# Monitor watchdog
tail -f /home/gomer/pythonCron/watchdog_daemon.log
```

### Phase 5: Decommission Old Scheduler

```bash
# Stop old scheduler
pkill -f scheduler.py

# Archive it
mv scheduler.py scheduler_legacy.py

# Keep for 2 weeks as emergency fallback
```

## Operational Guide

### Starting Services

**Manual**:
```bash
python3 service_wrapper.py <service_name> &
```

**Using Start Script**:
```bash
bash systemd/start_all_wrappers.sh
```

**Using Systemd**:
```bash
sudo systemctl start service-<name>.service
```

### Stopping Services

**Manual**:
```bash
pkill -f 'service_wrapper.py <service_name>'
```

**Using Stop Script**:
```bash
bash systemd/stop_all_wrappers.sh
```

**Using Systemd**:
```bash
sudo systemctl stop service-<name>.service
```

### Monitoring Services

**Check which services are running**:
```bash
ps aux | grep service_wrapper.py
```

**View service logs**:
```bash
tail -f /home/gomer/pythonCron/logs/<service_name>.log
```

**Check service state**:
```bash
cat /home/gomer/pythonCron/state/<service_name>.json | python3 -m json.tool
```

**Monitor watchdog**:
```bash
tail -f /home/gomer/pythonCron/watchdog_daemon.log | grep -E '(HEARTBEAT|ERROR|WARNING)'
```

**Check disk space**:
```bash
df -h /
du -sh /home/gomer/pythonCron/logs/*
```

### Troubleshooting

**Service not running**:
```bash
# Check state file
cat /home/gomer/pythonCron/state/<service_name>.json

# Check wrapper process
ps aux | grep 'service_wrapper.py <service_name>'

# Manually restart
python3 service_wrapper.py <service_name> &

# Check logs for errors
tail -100 /home/gomer/pythonCron/logs/<service_name>.log
```

**Service repeatedly failing**:
```bash
# Check consecutive failures in state
cat /home/gomer/pythonCron/state/<service_name>.json | grep consecutive_failures

# Review logs for error patterns
grep ERROR /home/gomer/pythonCron/logs/<service_name>.log | tail -20

# Check if script itself has issues
/usr/bin/python3 /path/to/script.py  # Run manually
```

**Watchdog not restarting services**:
```bash
# Check watchdog is running
ps aux | grep watchdog_daemon.py

# Check watchdog logs
tail -50 /home/gomer/pythonCron/watchdog_daemon.log

# Verify auto_restart is enabled in config
cat services_config.json | python3 -m json.tool | grep -A 5 watchdog
```

**Disk space issues**:
```bash
# Run emergency cleanup
python3 emergency_log_cleanup.py

# Check log sizes
du -sh /home/gomer/pythonCron/logs/*.log | sort -h

# Manually delete old logs if needed
rm /home/gomer/pythonCron/logs/*.log.[2-9]
```

## Configuration Tips

### Scheduling

**Interval-based** (runs every N seconds):
```json
"execution": {
  "type": "interval",
  "interval_seconds": 3600,
  "jitter_seconds": 60
}
```

**Time-based** (runs at specific time daily):
```json
"execution": {
  "type": "time",
  "interval_seconds": 86400,
  "time": "22:00",
  "jitter_seconds": 120
}
```

### Timeouts

Set appropriate timeouts based on typical execution time:
- Short scripts (<1min): 300s (5 min)
- Medium scripts (1-10min): 1800s (30 min)
- Long scripts (10-30min): 3600s (1 hour)
- Very long scripts (30min-1.5hr): 5400s (90 min)

### Retries

**Fast-failing services** (network checks):
```json
"retry": {
  "enabled": true,
  "max_retries": 5,
  "retry_delay_seconds": 10,
  "exponential_backoff": false
}
```

**Expensive operations** (database backups):
```json
"retry": {
  "enabled": true,
  "max_retries": 2,
  "retry_delay_seconds": 300,
  "exponential_backoff": true
}
```

### Logging

**High-frequency services**:
```json
"logging": {
  "level": "WARNING",
  "max_size_mb": 20,
  "max_files": 2
}
```

**Important services**:
```json
"logging": {
  "level": "INFO",
  "max_size_mb": 100,
  "max_files": 5
}
```

## Benefits Over Old Architecture

| Feature | Old (scheduler.py) | New (Wrappers) |
|---------|-------------------|----------------|
| **Failure Isolation** | One crash affects all | Isolated per service |
| **Recovery Time** | Hours (manual) | Minutes (automatic) |
| **Observability** | Single log file | Per-service logs + state |
| **Complexity** | 1000+ lines | ~200 lines per component |
| **Deadlocks** | Common (pipe buffers) | Eliminated (temp files) |
| **Resource Limits** | Global only | Per-service |
| **Debugging** | Difficult | Clear boundaries |
| **Adding Services** | Modify central code | Just config change |
| **Thread Pool** | Exhaustion risk | Not needed |
| **State Management** | Single JSON file | Per-service files |

## Maintenance

### Weekly

- Check watchdog logs for patterns
- Review disk space usage
- Check for services with high failure rates

### Monthly

- Review and tune timeout settings
- Analyze service execution times
- Clean up old rotated logs
- Review and update service configurations

### Quarterly

- Review overall architecture effectiveness
- Consider consolidating similar services
- Update documentation based on learnings

## Support

For issues or questions:
1. Check logs in `/home/gomer/pythonCron/logs/`
2. Review state files in `/home/gomer/pythonCron/state/`
3. Consult watchdog logs in `/home/gomer/pythonCron/watchdog_daemon.log`
4. Refer to this documentation

## Files Reference

```
/home/gomer/pythonCron/
├── config.json                      # Old configuration (deprecated)
├── scheduler.py                     # Old scheduler (deprecated)
├── scheduler_legacy.py              # Archived old scheduler
├── services_config.json             # New configuration
├── service_wrapper.py               # Wrapper template
├── watchdog_daemon.py               # Watchdog process
├── wrapper_generator.py             # Generator utility
├── emergency_log_cleanup.py         # Cleanup utility
├── README_NEW_ARCHITECTURE.md       # This file
├── logs/                            # Per-service logs
│   ├── Check_Disk.log
│   ├── MySQL_Backup.log
│   └── ...
├── state/                           # Per-service state
│   ├── Check_Disk.json
│   ├── MySQL_Backup.json
│   └── ...
└── systemd/                         # Generated service files
    ├── service-check_disk.service
    ├── start_all_wrappers.sh
    └── stop_all_wrappers.sh
```
