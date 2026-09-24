# signlab_pythonCron
Job scheduler and monitors for the SignCollect servers: media conversion, mocap matching, database syncs, backups and disk checks.

## What it does
- It has two schedulers, and the core server runs both.
  - `scheduler_v2.py` is one process with a 60 s loop and SQLite state (`lib/`). It reads `config.json`.
  - `service_wrapper.py <name>` runs one job per process and systemd unit. `watchdog_daemon.py` supervises the wrappers. They read `services_config.json`.

  | Entry point | Reads | Runs on |
  |---|---|---|
  | `python-scheduler.service` -> `scheduler_v2.py` | `config.json` (`--config`) | core server; demo hosts, with the stack's `config/pythoncron.demo.json` in its place |
  | 19 `service-<job>.service` units and `watchdog-daemon.service` -> `service_wrapper.py`, `watchdog_daemon.py` | `services_config.json` | core server only |
- 16 of the 17 `config.json` jobs are also enabled in `services_config.json`, so the core server runs them twice. Only in `config.json`: Cleanup OBS. Only in `services_config.json`: Copy_AB_files, match_Records_for_LiveLink..., qRconvert. Dropping one side changes what the core server runs; decide that with the switch-over in [stack#33](https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-stack/issues/33). Pick one owner before you add a job.
- Monitors: `server_monitor.py` (disk, rclone mount, MySQL; alerts to Discord), `check_disk.py` (mail through Mailjet; `checkDisk.py` is a stub with its old name), `rclone_monitor.py`, `sync_mocap_files.py`. Disk and mount checks, alerts and rotating logs come from the shared client (see Dependencies).

## Where it runs
| Host | Code | Job list | State and logs |
|---|---|---|---|
| core server | `/home/gomer/pythonCron` | `config.json`, `services_config.json` there | same folder |
| demo (`dev2`, `dev-1`) | `/opt/pythonCron` (owned by root) | `/etc/opt/pythonCron/config.json` | `/var/opt/pythonCron` |

The core server runs its own checkout, which may differ from this repo ([stack#33](https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-stack/issues/33)).

## Status
Production. Config changes run unattended on real data.

## How to run / deploy
```bash
python3 scheduler_v2.py --config <config.json> --validate-config   # check only, run nothing
python3 scheduler_v2.py --dry-run
python3 service_wrapper.py Check_Disk        # one wrapper in the foreground
python3 wrapper_generator.py                 # regenerate systemd/service-*.service
python3 -m pytest tests/ -v
```
- Demo hosts: `interface_deploy/scripts/pythoncron.sh` in [signlab_signcollect-stack](https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-stack). It installs one job (Signbank ECV refresh, `config/pythoncron.demo.json`). It also symlinks `/opt/pythonCron/config.json` to `/etc/opt/...`, so the core server's job list can never run there.
- Never add this repo to `repos.tsv`. That would put it in the docroot and publish its source over HTTP.
- Core server: units are installed by hand: `python-scheduler.service`, `server-monitor.service`, `watchdog-daemon.service`, and `bash setup_systemd_services.sh` for the 19 wrappers.
- Operations: `systemctl list-units 'service-*'` and `journalctl -u python-scheduler.service -f` (or `-u server-monitor`). Logs: `scheduler_v2.log` and `watchdog.log` (rotated at 5 MB, 5 files), `logs/<name>.log`. State: `state/<name>.json`. Never `rm` a live log; empty it with `: > logs/<name>.log`.

## Configuration
- `config.json` is a flat JSON array of jobs. Keys: `service_name` (unique), `executable`, `path` (job skipped if missing), `working_dir`, `interval_minutes`, `time_or_minute` (`minute` or `time`), `scheduled_time` (`HH:MM`), `timeout_minutes`, `execute_immediately`.
- `services_config.json` is `{global, services: [...]}`. Each job has `execution`, `command`, `health`, `retry`, `logging`.
- All job paths are absolute and host-specific (`/web`, `/web/zin`, `/home/gomer/viconSync`).
- `check_disk.py`, `server_monitor.py` and `sync_mocap_files.py` find the docroot with the vendored `sc_paths.py`: `SC_WEB_ROOT` from the environment or `$SC_ENV_FILE`/`/web/.env`, default `/web`. Edit it in [signlab_signcollect-lib](https://github.com/Amsterdam-Humanities-Labs/signlab_signcollect-lib), not here.
- `PYTHONCRON_HOME` (config lookup) and `PYTHONCRON_STATE_DIR` (logs, `scheduler_state.db`) relocate the v2 scheduler. The wrappers and the watchdog still hardcode `/home/gomer/pythonCron`.
- Keep `scheduler_state.db`. Without it, every job is due at once.
- Secrets come from `/web/zin/.env` (not in git): `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DISCORD_WEBHOOK_URL`, `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `MAILJET_API_KEY`, `MAILJET_SECRET_KEY`.
- Git ignores `logs/`, `state/` and `*.db`.

## Dependencies
- It runs files from [signlab_zinnen-annotation](https://github.com/Amsterdam-Humanities-Labs/signlab_zinnen-annotation), [signlab_mocap](https://github.com/Amsterdam-Humanities-Labs/signlab_mocap), [signlab_viconSync](https://github.com/Amsterdam-Humanities-Labs/signlab_viconSync), loose docroot scripts (`/web/cleanLockFiles.php`, `/web/tempScripts/`, `/web/josBoard/`, `/web/qr/`) and, on the demo hosts, [signlab_signCollect-v2](https://github.com/Amsterdam-Humanities-Labs/signlab_signCollect-v2) (`signbank_sync/ecv_refresh.php`, which decides itself whether a rebuild is due). The units need write access to the docroot.
- [signlab_client_monitor_api](https://github.com/Amsterdam-Humanities-Labs/signlab_client_monitor_api): `python_client.py` is a vendored copy of its `client/` package; do not edit it here. Scripts import `ClientMonitor`, `disk_usage`, `mount_responds`, `mount_read_write`, `send_alert` and `setup_rotating_logger` from the installed `signlab-client-monitor` package (1.1.0 or later), else from this copy. Heartbeats go to `https://signcollect.nl/client_monitor_api/api.php`. Keep the vendored copy until the core server has the package.
- External services: MySQL, Discord, Mailjet.

## License and citation

Apache License 2.0, copyright University of Amsterdam: see [LICENSE](LICENSE) and
[NOTICE](NOTICE). You may use it, also commercially, as long as you credit
Gomer Otterspeer / University of Amsterdam as the source. To cite it, use
[CITATION.cff](CITATION.cff) (the *Cite this repository* button on GitHub) or the DOI [10.21942/uva.33980362](https://doi.org/10.21942/uva.33980362).
