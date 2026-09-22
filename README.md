# signlab_pythonCron
Job scheduler and monitors for the SignCollect servers: media conversion, mocap matching, DB syncs, backups, disk checks.

## What it does
- **Two schedulers, both active on production.** `scheduler_v2.py` (one process, 60 s loop, SQLite state via `lib/`) reads `config.json`. `service_wrapper.py <name>` (one process and systemd unit per job, supervised by `watchdog_daemon.py`) reads `services_config.json`.
- 12 of the 17 `config.json` jobs are also in `services_config.json`, so they run twice. Pick one owner before adding a job. The intended end state is the wrappers alone.
- Monitors: `server_monitor.py` (disk, rclone mount, MySQL, alerts to Discord), `checkDisk.py` (Mailjet mail), `rclone_monitor.py`, `sync_mocap_files.py`.

## Where it runs
| Host | Code | Job list | State + logs |
|---|---|---|---|
| production (signcollect core) | `/home/gomer/pythonCron` | `config.json`, `services_config.json` there | same dir |
| demo (`dev2`, `dev-1`) | `/opt/pythonCron` (root-owned) | `/etc/opt/pythonCron/config.json` | `/var/opt/pythonCron` |

Production runs its own checkout, which may differ from this repo ([stack#33](https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-stack/issues/33)).

## Status
Production. Config edits run unattended on real data.

## How to run / deploy
```bash
python3 scheduler_v2.py --config <config.json> --validate-config   # check, run nothing
python3 scheduler_v2.py --dry-run
python3 service_wrapper.py Check_Disk        # one wrapper in the foreground
python3 wrapper_generator.py                 # regenerate systemd/service-*.service
python3 -m pytest tests/ -v
```
- Demo hosts: `interface_deploy/scripts/pythoncron.sh` in the [stack repo](https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-stack). It installs one job (Signbank ECV refresh, `config/pythoncron.demo.json`) and symlinks `/opt/pythonCron/config.json` to `/etc/opt/...`, so production's list can never run there.
- Never add this repo to `repos.tsv`: that would put it in the docroot and publish its source over HTTP.
- Production: units installed by hand (`python-scheduler.service`, `server-monitor.service`, `watchdog-daemon.service`, `bash setup_systemd_services.sh` for the 16 wrappers).
- Ops: `systemctl list-units 'service-*'`, `journalctl -u python-scheduler.service -f`, `logs/<name>.log`, `state/<name>.json`. Never `rm` a live log; truncate it with `: > logs/<name>.log`.

## Configuration
- `config.json`: flat JSON array of jobs. Keys: `service_name` (unique), `executable`, `path` (missing = job skipped), `working_dir`, `interval_minutes`, `time_or_minute` (`minute`|`time`), `scheduled_time` (`HH:MM`), `timeout_minutes`, `execute_immediately`.
- `services_config.json`: `{global, services: [...]}`; each job has `execution`, `command`, `health`, `retry`, `logging`.
- All job paths are absolute and host-specific (`/web`, `/web/zin`, `/home/gomer/viconSync`).
- `checkDisk.py`, `server_monitor.py`, `sync_mocap_files.py` resolve the docroot with vendored `sc_paths.py` (`SC_WEB_ROOT` env or in `$SC_ENV_FILE`/`/web/.env`, default `/web`). Edit it in signlab_signcollect-lib, not here.
- `PYTHONCRON_HOME` (config lookup) and `PYTHONCRON_STATE_DIR` (logs, `scheduler_state.db`) move the v2 scheduler. Wrappers and watchdog still hardcode `/home/gomer/pythonCron`.
- Keep `scheduler_state.db`: without it every job counts as due at once.
- Secrets come from `/web/zin/.env` (not in git): `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_WEBHOOK_URL`, `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`.
- `logs/`, `state/`, `*.db` are gitignored.

## Dependencies
- Runs files from `signlab_zin`, `signlab_mocap`, the docroot `helpScripts/` and `signlab_signCollect-v2` (`signbank_sync/ecv_refresh.php`, which decides for itself whether a rebuild is due). The unit needs write access to the docroot.
- `signlab_client_monitor_api`: `python_client.py` is a vendored copy of its `client/` package. Do not edit it here. `checkDisk.py`, `rclone_monitor.py`, `sync_mocap_files.py` prefer the installed `signlab-client-monitor` package and heartbeat to `https://signcollect.nl/client_monitor_api/api.php`. Shared checks and alerting are planned to move into that package ([stack#37](https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-stack/issues/37)); keep the vendored copy until production has the package.
- External: MySQL, Discord, Mailjet.
