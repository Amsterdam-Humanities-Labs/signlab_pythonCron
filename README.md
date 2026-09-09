# pythonCron

Service scheduling and monitoring for the Zin / SignCollect infrastructure. It runs
recurring jobs (media conversion, mocap file matching, database syncs, backups, disk
checks) on a fixed schedule, tracks their state, and restarts them when they fail.

> **This repository mirrors a live production system.** The code here runs under systemd
> on the production host and drives real data pipelines. Read [Operational notes](#operational-notes)
> before running anything.

## Where it runs, and status

**Status: production.** This scheduler executes real jobs against real data on
the **signcollect core server** — database backups, media conversion, mocap
file matching, the Signbank gloss refresh. A mistake in a config entry here is
a mistake that runs, unattended, on a schedule.

Two installations exist, and they are laid out differently:

| Host | Code | Job list | State and logs |
|---|---|---|---|
| signcollect core server (production) | `/home/gomer/pythonCron` | `config.json` and `services_config.json` in that directory | the same directory |
| demo hosts (`dev2`, `dev-1`) | `/opt/pythonCron` | `/etc/opt/pythonCron/config.json` | `/var/opt/pythonCron` |

The three-way split on the demo hosts is the FHS layout for an add-on
application: `/opt/<name>` for the package, `/etc/opt/<name>` for its host
configuration, `/var/opt/<name>` for its variable data. It earns its keep
beyond tidiness — `/opt/pythonCron` is root-owned and replaced wholesale by
every deploy, so the service user cannot rewrite its own code, and
`scheduler_state.db` survives in `/var/opt/pythonCron` because the deploy never
touches it. Losing that database makes every job read as never-executed and
therefore due immediately, which for this job set means an unscheduled
7,500-request rebuild against a third-party service.

`PYTHONCRON_HOME` and `PYTHONCRON_STATE_DIR` (see [Operational notes](#operational-notes))
are what make that split possible. Production predates them and still runs out
of one directory.

TODO: confirm whether production is expected to move to the `/opt` layout, or
to stay at `/home/gomer/pythonCron`.

## Deployment

**This is not a docroot component, and it must never become one.**

Every web component of this estate is deployed by
`signlab_signcollect-stack`'s `interface_deploy/`, from a row in
`scripts/repos.tsv` that maps a repository to `<docroot>/<directory>`. That is
the only destination the bootstrap knows. A `repos.tsv` row for pythonCron
would therefore clone a systemd service into the web root and **publish its
source over HTTP** — the config files, the job paths, the wrapper scripts,
`.env` handling and all. It is not merely the wrong tool here; it is a
disclosure bug.

So it gets its own installer, `interface_deploy/scripts/pythoncron.sh`, which:

1. clones this repository on the host into a build directory *outside* the
   docroot, and rewrites production hostnames out of it;
2. copies the tree into root-owned `/opt/pythonCron`;
3. installs this host's job list at `/etc/opt/pythonCron/config.json` and
   replaces `/opt/pythonCron/config.json` with a symlink to it;
4. renders `python-scheduler.service` from a template and enables it.

On production the same unit is installed by hand from
`python-scheduler.service` in this repository (and the sixteen `service-*`
wrapper units from `setup_systemd_services.sh`). Either way the deployment
artefact is a **systemd unit**, not a URL.

### Production's `config.json` is deliberately not used on demo hosts

The demo installs exactly one job — the Signbank ECV refresh — from
`interface_deploy/config/pythoncron.demo.json`, with the docroot
parameterised. Production's own seventeen-entry `config.json` is not copied,
and the reason is specific rather than cautious: three of its jobs
(`mocap/matchVicon.py`, `mocap/convert.py`, `zin/syncEafToDatabase.php`) name
paths that **also exist on a demo host**, so `scheduler_v2` would find them,
consider them valid, and start running them against demo data unasked. The
validator cannot help — a path that exists is a path that passes.

That is also why `/opt/pythonCron/config.json` is replaced by a symlink to
`/etc/opt/pythonCron/config.json`: even a hand-run `python3 scheduler_v2.py`
from inside the code directory cannot pick up a checked-in production job list.

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

`config.json` is a **flat JSON array** — no wrapper object, no keys above it — and
each element is one job. Where the file lives depends on the host: the directory
holding `scheduler_v2.py` by default, `PYTHONCRON_HOME` if set, or whatever
`--config` names (the demo passes `/etc/opt/pythonCron/config.json`).

| Field | Required | Meaning |
|---|---|---|
| `service_name` | yes | Display name and state key. Must be unique — a duplicate is a validation error, and the second entry is dropped. |
| `executable` | yes | Absolute path to the interpreter, e.g. `/usr/bin/php`, `/usr/bin/python3`. A missing or non-executable path is a *warning*, not an error. |
| `path` | yes | Absolute path to the script to run. A path that does not exist is a hard **error** and the job is skipped. |
| `working_dir` | yes | Directory to run it in. Must exist. |
| `interval_minutes` | yes | Positive number. With `time_or_minute: "minute"`, how often to run. |
| `time_or_minute` | yes | `"minute"` (every `interval_minutes`) or `"time"` (once a day at `scheduled_time`). Anything else is an error. |
| `scheduled_time` | with `"time"` | `HH:MM`, 24-hour. Required and format-checked when `time_or_minute` is `"time"`. |
| `timeout_minutes` | no | Positive number; the job is killed past it. |
| `execute_immediately` | no | Run once at scheduler start rather than waiting out the first interval. |

Validate before trusting it — this reports every error and warning and runs nothing:

```bash
python3 scheduler_v2.py --config /etc/opt/pythonCron/config.json --validate-config
```

**Every path in a `config.json` entry is absolute and host-specific**, which is
what makes a job list non-portable between hosts and is the reason the demo
writes its own rather than shipping production's — see
[Deployment](#deployment). Nothing in the format identifies the host it was
written for, and the validator's only test is whether the path exists.

`services_config.json` is the other architecture's file and is not
interchangeable with this one: it is keyed by service name and nests
`execution`, `health`, `retry` and `logging` blocks per service. A job present
in both files is scheduled twice.

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
  architecture (`service_wrapper.py`, `watchdog_daemon.py`) still hardcodes
  `/home/gomer/pythonCron`; the standalone job scripts no longer do, since the
  only thing they used it for was importing the heartbeat client.
- **Logs and state are not in version control.** `logs/`, `state/`, and `scheduler_state.db`
  are gitignored; they are machine-local runtime data.
- **Logs grow very large** — individual files reach 50–100 MB and rotated archives had
  accumulated to hundreds of megabytes. Keep rotation healthy and use
  `emergency_log_cleanup.py` if disk pressure appears.
- **Never `rm` an active log file.** Running processes hold open handles, so the space is
  not reclaimed and the process keeps writing to a deleted inode. Truncate instead:
  `: > logs/Some_Service.log`

## Dependencies

pythonCron does not import anything from the rest of the estate — it launches
processes. The coupling runs the other way, and is by job list rather than by
code:

- **`signlab_signCollect-v2`** — its Signbank connector
  (`signbank_sync/ecv_refresh.php`) names pythonCron as its scheduler in its
  own header and prints the `config.json` entry it expects. That job rebuilds
  the shared gloss dump at `<docroot>/signbank_data/glosses_transformed.json`,
  which four other components read by absolute path. Running the job hourly is
  not the same as refreshing hourly: `ecv_refresh.php` decides for itself
  whether a rebuild is due, from a schedule an admin sets on the connector page
  and which defaults to off. That indirection is deliberate — `config.json`
  belongs to a root-owned systemd unit and the web server must not have to
  rewrite it.
- **`signlab_zin`, `signlab_mocap`, and the docroot's `helpScripts/`** — most
  of production's seventeen jobs are PHP or Python files inside the web root.
  The scheduler needs write access to it, which is why the unit relaxes
  `ProtectSystem` and names the docroot in `ReadWritePaths`.
- **Discord, Mailjet, MySQL** — `discord_bot.py`, `checkDisk.py` and
  `server_monitor.py` reach outward; all three read their credentials from the
  environment (see [Secrets](#secrets)).
- **`signlab_client_monitor_api`** — `python_client.py` here is a verbatim
  vendored copy of that repository's `client/` package, and `php_client.php`
  its PHP counterpart. The three scripts below prefer the installed
  `signlab-client-monitor` package and fall back to the vendored file beside
  them, so nothing here needs the package to be installed and nothing here
  needs `/home/gomer/pythonCron` on `sys.path` any more. Do not edit
  `python_client.py`: refresh it from the package, per its own header.
  `checkDisk.py`,
  `rclone_monitor.py` and `sync_mocap_files.py` each construct a
  `ClientMonitor` against `https://signcollect.nl/client_monitor_api/api.php`
  and register + heartbeat there, so those three jobs appear on the client
  monitor dashboard. The URL is hardcoded in each script; on a host that is
  firewalled from production the calls simply fail, which is why the demo
  installer rewrites production hostnames out of the checkout even though
  none of these three is scheduled there.

## Migration status

Both schedulers are active on the production host at the same time: 16 `service-*` wrapper
units plus `python-scheduler.service`. Before adding a service, confirm which system owns it
so a job is not scheduled twice from both `config.json` and `services_config.json`.
Consolidating onto the wrapper architecture is the intended end state.
