# Agent notes for signlab_pythonCron
README.md has commands, config keys, secrets and deploy. These are the traps.

## The production checkout is live
- `/home/gomer/pythonCron` on the core server runs under systemd right now. Editing `config.json` or `services_config.json` there changes running jobs immediately.
- Do not `rm` an active `.log` (open handles keep writing to the deleted inode). Truncate: `: > logs/Name.log`.
- Before deleting or renaming a file: `grep -rn "<filename>" config.json services_config.json systemd/`.

## Two schedulers run side by side
- Wrappers: `services_config.json` -> `service_wrapper.py <name>` (one systemd unit each, made by `wrapper_generator.py`), supervised by `watchdog_daemon.py`; state in `state/<name>.json`.
- Scheduler v2: `config.json` -> `scheduler_v2.py` -> `lib/` (state_manager, circuit_breaker, health_monitor, service_executor, config_validator) -> `scheduler_state.db`.
- Find which one owns a job before changing it. Adding a job to both files runs it twice.
- Names differ: `config.json` uses spaces ("Get Themas"); `services_config.json`, state and log files use underscores (`Get_Themas`).

## Paths
- `scheduler_v2.py` and `lib/` derive paths from the checkout (`PYTHONCRON_HOME`, `PYTHONCRON_STATE_DIR`). Do not reintroduce a literal `/home/gomer/pythonCron` there; `service_wrapper.py`, `watchdog_daemon.py` and the job scripts still hardcode it.
- Never hardcode credentials; they come from `/web/zin/.env`.
