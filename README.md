# pythonCron

Service scheduling and monitoring for the Zin / SignCollect infrastructure. It runs
recurring jobs (media conversion, mocap file matching, database syncs, backups, disk
checks) on a fixed schedule, tracks their state, and restarts them when they fail.

> **This repository mirrors a live production system.** The code here runs under systemd
> on the production host and drives real data pipelines. Read [Operational notes](#operational-notes)
> before running anything.

## Architecture

Two scheduling systems currently run **side by side**. This is deliberate but transitional —
see [Migration status](#migration-status).

### 1. Wrapper architecture (current direction)

Each service is its own long-lived process with its own systemd unit. A wrapper owns one
service: it sleeps, executes on schedule, enforces timeouts, retries, and writes health state.

```
services_config.json          # single source of truth
        │
        ├── service_wrapper.py <name>   ×16   (one process + systemd unit per service)
        │        └── writes → logs/<name>.log, state/<name>.json
        │
        └── watchdog_daemon.py                (monitors state files, restarts stuck services)
```

- `service_wrapper.py` — self-scheduling executor for a single service
- `watchdog_daemon.py` — supervises wrappers, restarts unhealthy ones
- `wrapper_generator.py` — generates the `systemd/service-*.service` units from config
- `services_config.json` — per-service execution, health, retry, and logging config

### 2. Centralized scheduler v2

A single process that loops every 60s and dispatches all due services, backed by SQLite.

```
config.json ──> scheduler_v2.py ──> lib/ ──> scheduler_state.db
```

| Module | Responsibility |
|---|---|
| `lib/state_manager.py` | Thread-safe SQLite state, ACID + locking |
| `lib/circuit_breaker.py` | Stops retrying persistently failing services |
| `lib/health_monitor.py` | Detects and kills stuck processes |
| `lib/service_executor.py` | Subprocess execution and error handling |
| `lib/config_validator.py` | Validates config before the scheduler starts |

### Supporting services

- `server_monitor.py` — disk, rclone mount, and MySQL monitoring; alerts to Discord
- `discord_bot.py` — reusable Discord notification module (bot token or webhook)
- `rclone_monitor.py`, `checkDisk.py`, `sync_mocap_files.py` — individual job scripts
- `emergency_log_cleanup.py` — reclaims disk when logs grow out of control

## Configuration

Two config files exist, one per architecture. **They are not interchangeable.**

| File | Consumed by | Shape |
|---|---|---|
| `services_config.json` | wrappers, watchdog, generator | nested: `execution`, `health`, `retry`, `logging` |
| `config.json` | `scheduler_v2.py` | flat list of services |

A `config.json` entry:

```json
{
  "service_name": "Get Themas",
  "executable": "/usr/bin/php",
  "path": "/web/zin/api/getThemas.php",
  "working_dir": "/web/zin/api",
  "interval_minutes": 60,
  "time_or_minute": "minute",
  "timeout_minutes": 30,
  "execute_immediately": true
}
```

`time_or_minute` selects the scheduling mode: `"minute"` runs every `interval_minutes`,
`"time"` runs daily at `scheduled_time` (e.g. `"22:30"`).

## Secrets

No credentials live in this repository. All secrets are read from the environment, loaded
from `/web/zin/.env` on the production host. Create that file with:

```ini
# Discord notifications (bot token + channel, or a webhook URL)
DISCORD_BOT_TOKEN=
DISCORD_CHANNEL_ID=
DISCORD_WEBHOOK_URL=

# MySQL connection used by server_monitor.py health checks
DB_HOST=localhost
DB_USER=user
DB_PASSWORD=
DB_NAME=admin_gebarenoverleg

# Mailjet, used by checkDisk.py to send low-disk alert emails
MAILJET_API_KEY=
MAILJET_SECRET_KEY=
```

`server_monitor.py` logs a warning and skips MySQL checks if `DB_PASSWORD` is unset.
Never commit a real `.env` — the ignore rules exclude it.

## Running

```bash
# Centralized scheduler (validate config without executing anything)
python3 scheduler_v2.py --dry-run

# A single service wrapper in the foreground
python3 service_wrapper.py Check_Disk

# Regenerate systemd units after editing services_config.json
python3 wrapper_generator.py

# Watchdog
python3 watchdog_daemon.py --check-interval 3600
```

### Tests

```bash
python3 -m pytest tests/ -v
```

`tests/test_scheduler_v2.py` covers the v2 library modules. The migration helper
`migrations/migrate_json_to_sqlite.py` converts legacy JSON state into `scheduler_state.db`.

## Operations

```bash
# Status
systemctl status python-scheduler.service
systemctl list-units 'service-*'

# Logs
journalctl -u python-scheduler.service -f
tail -f logs/<Service_Name>.log

# Health state per service
cat state/<Service_Name>.json
```

Install or refresh all units:

```bash
bash setup_systemd_services.sh
```

See `SYSTEMD_SETUP_INSTRUCTIONS.md` for the full procedure and
`README_NEW_ARCHITECTURE.md` for the wrapper design rationale.

## Operational notes

- **Service paths in the configs are absolute and host-specific.** `config.json` and
  `services_config.json` name `/web`, `/web/zin` and `/home/gomer/viconSync`; those are
  the jobs' own paths and a different host needs its own service list.
- **The scheduler itself is relocatable.** `scheduler_v2.py` and `lib/` resolve their
  config, SQLite state and log files from where the checkout actually lives, so
  `/home/gomer/pythonCron` is a default, not a requirement. Two environment variables
  override it:

  | Variable | Default | What it moves |
  |---|---|---|
  | `PYTHONCRON_HOME` | the directory holding `scheduler_v2.py` | `config.json` lookup |
  | `PYTHONCRON_STATE_DIR` | `PYTHONCRON_HOME` | `scheduler_v2.log`, `watchdog.log`, `scheduler_state.db`, per-service logs |

  Splitting the two lets a deploy replace the code directory wholesale without
  destroying the state that records when each service last ran. The wrapper
  architecture (`service_wrapper.py`, `watchdog_daemon.py`) and the standalone job
  scripts still hardcode `/home/gomer/pythonCron`.
- **Logs and state are not in version control.** `logs/`, `state/`, and `scheduler_state.db`
  are gitignored; they are machine-local runtime data.
- **Logs grow very large** — individual files reach 50–100 MB and rotated archives had
  accumulated to hundreds of megabytes. Keep rotation healthy and use
  `emergency_log_cleanup.py` if disk pressure appears.
- **Never `rm` an active log file.** Running processes hold open handles, so the space is
  not reclaimed and the process keeps writing to a deleted inode. Truncate instead:
  `: > logs/Some_Service.log`

## Migration status

Both schedulers are active on the production host at the same time: 16 `service-*` wrapper
units plus `python-scheduler.service`. Before adding a service, confirm which system owns it
so a job is not scheduled twice from both `config.json` and `services_config.json`.
Consolidating onto the wrapper architecture is the intended end state.
