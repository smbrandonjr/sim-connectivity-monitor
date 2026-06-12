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

- [x] Phase 0 — project scaffolding, config schema / ICCID matcher
- [x] Phase 1 — core engine (state machine, drivers, supervisor) against a simulated modem
- [x] Phase 2 — web UI
- [x] Phase 3 — hardware integration code (mmcli/nmcli wrappers, AT channel, vendor drivers, udev, installer)
- [ ] Phase 4 — validate routing, monitor egress, fallback test on real hardware
- [ ] Phase 5 — resilience hardening on real hardware (USB power-cycle, watchdog tuning)

All logic is unit-tested on any OS; phases 4–5 are validation passes on a real Pi + modem
using the [smoke-test checklist](#smoke-test-checklist) below.

---

# Getting started

## What you need

- Raspberry Pi 3/4/5 (or Zero 2 W) with an SD card
- **Raspberry Pi OS (Debian 13 "Trixie")**, any variant — Lite is fine
- A supported USB cellular modem: Quectel (EC25/EG25-G/EC21/BG96), SIMCOM (SIM7600),
  or Telit (LE910) — others mostly work via the generic 3GPP driver
- An **activated** Hologram SIM (activate it in the Hologram dashboard first)
- Antennas connected to the modem (main + diversity if present)

## 1. Flash the Pi

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your computer.
2. Choose *Raspberry Pi OS (other)* → a **Trixie**-based image (Lite recommended for headless).
3. Click the gear / "Edit settings" before writing and set:
   - hostname (e.g. `sim-pi-01`), username + password
   - **enable SSH**
   - Wi-Fi credentials *(optional — handy for setup; cellular will outrank it later)*
4. Write the card, boot the Pi, and SSH in:

   ```sh
   ssh youruser@sim-pi-01.local
   ```

## 2. Install sim-monitor

```sh
sudo apt update && sudo apt install -y git
git clone https://github.com/smbrandonjr/sim-connectivity-monitor.git
cd sim-connectivity-monitor
sudo ./install.sh
```

The installer:

- installs NetworkManager, ModemManager, and a Python venv under `/opt/sim-monitor`
- writes `/etc/sim-monitor/config.yaml` and the default Hologram profile to
  `/etc/sim-monitor/profiles.d/` (existing files are never overwritten — re-running
  upgrades the code only)
- installs udev rules that reserve one spare AT serial port on the modem for sim-monitor
  (`/dev/sim-monitor-at`) and hide it from ModemManager
- installs and starts the `sim-monitor` systemd service (starts on every boot)

## 3. Insert the SIM and plug in the modem

1. Power off if your modem/HAT requires it; insert the SIM (note the orientation).
2. Connect the modem via USB and power up.
3. If the modem was already plugged in during install, unplug/replug it once so the
   new udev rules take effect.

Verify the system sees everything:

```sh
mmcli -L                      # should list your modem
ls -l /dev/sim-monitor-at     # should point at a ttyUSB port
systemctl status sim-monitor  # should be active (running)
journalctl -u sim-monitor -f  # watch it walk NO_MODEM -> ... -> CONNECTED
```

## 4. Open the web UI

Browse to `http://<pi-address>:8080` from your LAN.

- **Dashboard** — live state, ICCID/IMEI/operator/signal, IP, default-route check,
  and action buttons (reconnect, reset modem, run monitor probe, fallback test).
- **Profiles** — view/create/edit/delete profiles, force one for testing.
- **Monitor** — heartbeat history. **Events** — everything the daemon did.

> The UI has no authentication — keep it LAN-only. Don't port-forward it.

## 5. Confirm cellular is the default route

With the default profile connected:

```sh
ip route show default
```

The route via `wwan0` should have metric **50** (beats ethernet's 100 / Wi-Fi's 600).
Pull the ethernet cable and your SSH session via Wi-Fi/LAN stays up while internet
traffic flows over cellular; the dashboard's "Default route OK" should read *yes*.

## 6. Create your own profiles

The committed default (`00-hologram-default.yaml`) matches any SIM and uses a single
IPv4 context with APN `hologram`. For SIMs that need more, add profiles via the web UI
or drop YAML files into `/etc/sim-monitor/profiles.d/` (see
`config/profiles.d/50-custom.yaml.example` for every field):

```yaml
name: three-context-sims
match:
  iccid_patterns: ["8944502*"]     # exact ICCID or trailing-* prefix
  priority: 10                     # lower wins ties; default profile is 1000
pdp_contexts:                      # the modem will have EXACTLY these (1-3)
  - { cid: 1, apn: hologram, pdp_type: IPv4v6, bearer: true }
  - { cid: 2, apn: hologram.special, auth: pap, username: u, password: p }
  - { cid: 3, apn: ims, pdp_type: IPv4v6 }
monitor:
  enabled: true
  interval_seconds: 300
  request:
    method: POST
    url: "https://hooks.example.com/heartbeat"
    headers: { Authorization: "Bearer YOUR_TOKEN" }
    body: '{"iccid":"{iccid}","rssi":{signal_rssi},"ip":"{ip_address}","ts":"{timestamp}"}'
```

Files added by hand need a `Reload` — easiest is editing anything in the web UI, or
`sudo systemctl restart sim-monitor`. **These files can hold secrets and live only on
the device; never commit them** (the repo gitignores them).

Available monitor placeholders: `{iccid} {imei} {imsi} {operator} {signal_rssi}
{signal_percent} {ip_address} {interface} {hostname} {timestamp} {state} {profile_name}`.
On the Pi, the heartbeat socket is **bound to the cellular interface**, so a success
proves cellular egress even while ethernet is connected.

## 7. Test Hologram fallback / outage protection

On the dashboard, set a duration (default 900 s = 15 min) and click **Start fallback
test**. The daemon puts the modem in airplane mode, shows a countdown, then re-enables
the radio, re-reads the ICCID (the SIM applet may have switched profiles), re-matches
your profiles, and reconnects. The Events page logs the before/after identity.

## Operating

```sh
journalctl -u sim-monitor -f          # live logs
sudo systemctl restart sim-monitor    # restart the daemon
cd sim-connectivity-monitor && git pull && sudo ./install.sh   # upgrade
```

Recovery is automatic and layered: the internal supervisor escalates
reconnect → modem disable/enable → AT reset → USB power-cycle with backoff (it never
reboots the Pi), systemd's watchdog restarts the process if it ever hangs, and
`StartLimit` stops genuine crash loops.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `mmcli -L` shows no modem | Check `lsusb` for the modem; check power (modems are power-hungry — use a good PSU); try another cable/port. |
| Dashboard stuck in `NO_MODEM`, events say "no AT port was found" | Your modem model isn't in the udev rules / hints. Find the spare AT interface: `udevadm info -q property -n /dev/ttyUSB3 \| grep ID_USB_INTERFACE_NUM`, check `mmcli -m 0` "Ports" doesn't list it, then either add a rule to `/etc/udev/rules.d/77-sim-monitor.rules` (pattern inside) or set `modem.at_port: /dev/ttyUSB3` in `/etc/sim-monitor/config.yaml`. |
| MM claimed the port we want | Confirm the udev rule's `ID_USB_INTERFACE_NUM` matches and replug the modem; `mmcli -m 0` should no longer list that ttyUSB under Ports. |
| `SIM locked (SIM PIN)` in last error | Remove the PIN with another device/modem tool; PIN-locked SIMs are unsupported. |
| Connects but ethernet still wins the default route | Dashboard "Default route OK" will be *no* and the daemon re-asserts metrics; check `nmcli -f ipv4.route-metric connection show sim-monitor-cellular`. |
| Heartbeat fails while connected | The probe egresses via cellular only — check signal/serving network on the dashboard; check the URL from another network. |

## Smoke-test checklist

Run through this once per new modem model / OS image:

1. Clean `install.sh` on stock Trixie → `systemctl status sim-monitor` active.
2. Dashboard ICCID/IMEI/operator/signal match `mmcli -m 0` output.
3. `mmcli -m 0` Ports list does **not** include the `/dev/sim-monitor-at` target; AT reads
   work while MM is connected (no port fighting in the logs).
4. `ip route show default`: cellular metric 50 wins; web UI + SSH still reachable via LAN.
5. Pull ethernet → traffic flows via cellular; restore → cellular stays preferred.
6. Heartbeat arrives at your endpoint with the **cellular** source IP while ethernet is up.
7. `sudo mmcli -m 0 --command='AT+CGDCONT?'`… or check Events: PDP contexts equal the
   profile exactly (extras deleted on connect).
8. 2-minute fallback test round-trips; Events show before/after ICCID.
9. Hot-swap SIMs while running → new profile applied automatically.
10. `sudo mmcli -m 0 --disable` → supervisor recovers without a service restart.
11. `sudo kill -STOP $(pidof -x python | head -1)` (or the sim-monitor PID) → systemd
    watchdog restarts it once; no restart loop.
12. Reboot → reconnects unattended within ~1–2 min of boot.

---

## Development

Everything except real hardware runs anywhere (including Windows) via simulate mode:

```sh
python -m pip install -e .[dev]
python -m pytest
python -m sim_monitor --simulate
```

`--simulate` runs the full app (daemon + monitor + web UI on :8080) against a scripted
fake modem and network backend. Pure logic (AT parsing, ICCID matching, PDP diffing,
placeholder rendering) has no I/O and is unit-tested; hardware access sits behind
driver/backend interfaces with fakes.

## License

MIT
