# sim-connectivity-monitor

Keep a Raspberry Pi reliably online over cellular. Designed for [Hologram](https://www.hologram.io/)
SIMs and USB cellular modems from **Quectel, SIMCOM, and Telit**.

Install it on a Pi, plug in a modem, and it will:

- Detect the modem and read the SIM's **ICCID**
- Apply a matching **configuration profile** (PDP contexts, AT init commands, routing prefs) —
  with a built-in default for standard Hologram SIMs (APN `hologram`)
- Enforce **exactly** the PDP contexts the profile defines (modem-auto-created extras are removed)
- Connect via ModemManager/NetworkManager and make the cellular interface (`wwan0`) the
  **default, prioritized route** (LAN/SSH access stays intact)
- Send a configurable **HTTP heartbeat** out the cellular interface with SIM-specific
  placeholders (`{iccid}`, `{imei}`, `{signal_rssi}`, …)
- Serve a **LAN web UI** for live status, profile management, and manual actions
- Support **Hologram fallback/outage-protection testing** (airplane mode for ~15 min so the
  SIM applet switches profiles, then reconnect and observe the new identity)
- Handle **hot SIM swaps** (ICCID change → automatic re-match and reconnect)
- Recover failures with an escalation ladder (reconnect → modem disable/enable → AT reset →
  USB power-cycle) — without ever rebooting the Pi or thrashing in restart loops

## Status

In development. Phases:

- [x] Phase 0 — project scaffolding, config schema / ICCID matcher
- [ ] Phase 1 — core engine (state machine, drivers, supervisor) against a simulated modem
- [ ] Phase 2 — web UI
- [ ] Phase 3 — hardware integration (Pi + real modems)
- [ ] Phase 4 — routing, HTTP monitor, fallback test on hardware
- [ ] Phase 5 — resilience hardening + docs

## Target platform

Raspberry Pi OS (Debian 13 / Trixie) with NetworkManager + ModemManager. Python 3.11+.

## Development

Everything except real hardware runs anywhere (including Windows) via simulate mode:

```sh
python -m pip install -e .[dev]
python -m pytest
python -m sim_monitor --simulate
```

## Configuration

App config: `config.yaml` (see `config/config.example.yaml`).
Profiles: one YAML file per profile in `profiles.d/` (see
`config/profiles.d/00-hologram-default.yaml` and `50-custom.yaml.example`).
Profiles are matched to the inserted SIM by ICCID pattern (exact or trailing-`*` prefix),
with priority tie-breaks; a catch-all default covers standard Hologram SIMs.

> **Note:** real config files can contain secrets (webhook tokens, APN credentials) and are
> gitignored. Never commit them.

## License

MIT
