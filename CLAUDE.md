# sim-connectivity-monitor

Python daemon + web UI that guarantees cellular connectivity on Raspberry Pis using
Hologram SIMs and USB cellular modems (Quectel, SIMCOM, Telit). It recognizes the
inserted SIM's ICCID, applies a matching configuration profile (PDP contexts, AT init,
routing), connects via ModemManager/NetworkManager, makes the cellular interface the
default route, and heartbeats a configurable HTTP endpoint to prove connectivity.

## Architecture

- **Single process, three threads** (`src/sim_monitor/app.py` is the composition root):
  - *Daemon thread* — tick-based state machine (`core/daemon.py`), sole owner of all
    modem/network mutations.
  - *Monitor thread* — HTTP heartbeat scheduler (`monitor/http_monitor.py`), read-only.
  - *Main thread* — waitress serving Flask (`web/`), LAN-only: a JSON API
    (`web/routes/api.py`) plus the built Svelte SPA (`web/spa/`, served by
    `web/routes/spa.py`). The SPA source lives in `frontend/` (Svelte+Vite+TS);
    its build output is committed so the Pi never needs Node.
- **States**: `NO_MODEM → MODEM_FOUND → SIM_READY → CONFIGURING → CONNECTING → CONNECTED`,
  plus `DEGRADED` (supervisor escalating) and `FALLBACK_TEST`.
- **Web ↔ daemon**: Flask never touches hardware. It reads a lock-protected `StateStore`
  snapshot and enqueues `Command` objects on a `queue.Queue` the daemon drains each tick.
- **Hybrid modem control**: ModemManager/NetworkManager (`system/mmcli.py`, `system/nmcli.py`)
  own the connection lifecycle; raw AT commands over a dedicated serial port
  (`modem/at_channel.py`) handle nuanced ops (PDP reconciliation, airplane-mode fallback
  test, identity reads). udev rules hide that AT port from ModemManager (`ID_MM_PORT_IGNORE`).
- **Vendor drivers**: `modem/driver_base.py` ABC; `modem/at_driver.py` implements it with
  3GPP AT defaults; Quectel/SIMCOM/Telit subclasses (`modem/drivers/`) override quirks
  (ICCID command, reset command, PDP auth syntax). Selected by USB VID, fallback `AT+CGMI`
  (`modem/detect.py`). AT port resolution: config `modem.at_port` > `/dev/sim-monitor-at`
  udev symlink > VID/interface hints.
- **Hardware backend**: `system/real_backend.py` over `system/{mmcli,nmcli,routing}.py`
  subprocess wrappers (all parsing fixture-tested) + `system/usb_power.py` (sysfs
  `authorized` toggle). All subprocess calls timeout-bounded (`system/proc.py`).
- **PDP reconciliation**: the modem must end up with *exactly* the profile's `pdp_contexts`
  (1–3) — extras auto-created by modem firmware get deleted, mismatches fixed, missing ones
  defined. Pure diff logic in `modem/pdp_reconcile.py`.
- **Simulate mode** (`--simulate`): `FakeModemDriver` + `system/fake_backend.py` run the
  whole app on a dev machine (Windows) with no hardware.
- **Persistence**: SQLite via stdlib (`storage/db.py`) for events + monitor history.
  Config/profiles are YAML, validated with pydantic (`config/schema.py`).

## Dev commands

```sh
# Windows dev (PowerShell); venv lives in .venv
.venv\Scripts\python -m pip install -e .[dev]
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m sim_monitor --simulate          # run with fake modem (UI on :8080)
```

### Frontend (Svelte SPA)

The web UI is a single-page app in `frontend/` consuming the JSON API. The built
output is committed to `src/sim_monitor/web/spa/` so deployment needs no Node.
Rebuild after changing the frontend:

```sh
cd frontend && npm install && npm run build      # emits to ../src/sim_monitor/web/spa
# `npm run dev` runs Vite with HMR, proxying /api to a local --simulate app on :8080
```

Only `woff2` fonts are committed (legacy font formats are gitignored to keep the
repo small). Theme tokens/values come from the (git-ignored) theme guide; no
proprietary brand fonts are vendored — Inter + JetBrains Mono (OFL) + Remix Icons
(Apache) only.

## Deployment (Raspberry Pi, Raspberry Pi OS / Debian Trixie)

- `install.sh` sets up `/opt/sim-monitor` venv, `/etc/sim-monitor/` configs, udev rules,
  and the systemd unit (`deploy/sim-monitor.service`, `Type=notify` with watchdog).
- Runs as root (serial + nmcli/mmcli + `SO_BINDTODEVICE` + sysfs USB reset).
- Recovery is layered: internal supervisor with backoff/escalation (never exits, never
  reboots the Pi) → systemd `WatchdogSec` for hangs → `StartLimit*` to stop crash loops.
- Live config on the Pi: `/etc/sim-monitor/config.yaml` + `/etc/sim-monitor/profiles.d/*.yaml`.

## Conventions

- **This is a PUBLIC repo — never commit secrets.** Real configs (`config/config.yaml`,
  `config/profiles.d/*.yaml` except the secret-free Hologram default) are gitignored.
  Tokens, APN credentials, and webhook URLs belong only in local files on the device.
  Committed examples use placeholder values (`REPLACE_ME`, `example.com`).
- Commit at the end of every working session with a concise, useful message (no essays).
- Pure logic (AT parsing, ICCID matching, placeholder substitution, PDP diffing) stays
  free of I/O so it's unit-testable on any platform; hardware access lives behind
  driver/backend interfaces with fakes.
- Profile YAML files in `profiles.d/` are loaded in sorted filename order; one file per
  profile; invalid files are skipped with a logged event, never fatal.
