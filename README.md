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
- Serve a **LAN web UI** for live status, profile management, SMS (inbox/send),
  on-device network tools (host/port scan, reachability, traceroute), and manual actions
- Support **Hologram fallback/outage-protection testing** (airplane mode for ~15 min so the
  SIM applet switches profiles, then reconnect and observe the new identity)
- Handle **hot SIM swaps** (ICCID change → automatic re-match and reconnect)
- Recover failures with an escalation ladder (reconnect → modem disable/enable → AT reset →
  USB power-cycle) — without ever rebooting the Pi or thrashing in restart loops

Runs on real hardware (Raspberry Pi + USB modem) and on any OS via simulate mode. The
pure logic is unit-tested everywhere; the [smoke-test checklist](#smoke-test-checklist)
below is the pass to run when bringing up a new modem model or OS image.

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

- **Dashboard** — live state, ICCID/IMEI/operator/signal, IP / gateway / public IP,
  default-route check, serving-cell details (RAT/band/cell ID), signal telemetry charts,
  and action buttons (reconnect, reset modem, run monitor probe, fallback test).
- **Profiles** — view/create/edit/delete profiles, force one for testing.
- **Messages** — SMS inbox and compose/send over the modem.
- **Monitoring** — heartbeat configuration and recent heartbeat history.
- **Scan** — network tools (host discovery, port scan, reachability checks,
  traceroute), optionally bound to a specific interface.
- **Timeline** — everything the daemon did (events, URCs, identity changes).
- **Diagnostics** — AT command reference and ad-hoc AT command execution.

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
IPv4 context with APN `hologram`. Create more in the web UI's **Profiles** tab — a
structured form (no YAML required; a raw-YAML toggle is there for edge cases). A profile
covers ICCID match patterns, 1–3 PDP contexts (the modem is reconciled to *exactly*
these), optional alternative PDP variants tried in order, routing, and AT init commands.

Profiles can also be dropped as YAML files into `/etc/sim-monitor/profiles.d/`. **These
files can hold secrets and live only on the device; never commit them** (gitignored).

## 7. Monitoring (heartbeat)

Heartbeat monitoring is a **global** setting in the web UI's **Monitoring** tab — one
endpoint for all SIMs, with the recent heartbeat history shown below the form. (A profile
may override it for one SIM by enabling its own `monitor` block.) Configure the URL,
method, headers, body, interval, and the egress behavior:

Available placeholders (usable in URL, headers, body). Unknown values render empty /
are omitted from structured bodies:

- **Identity & state:** `{iccid} {imei} {imsi} {operator} {registration} {state}
  {profile_name} {sim_name} {status} {status_message} {last_error}`
- **Modem:** `{vendor} {model} {modem_model} {firmware}`
- **Signal:** `{signal_rssi}` (alias `{rssi}`) `{signal_percent}`
- **Serving cell (when reported):** `{rat} {rsrp} {rsrq} {sinr} {band} {earfcn}
  {cell_id} {tac} {pci} {mcc} {mnc} {operator_numeric} {channel}`
- **Network:** `{ip_address} {gateway} {public_ip} {interface} {apn}` plus per-interface
  IPs `{eth0_ip} {wlan0_ip} {wwan0_ip}` (only for interfaces that are up)
- **Host:** `{hostname} {uptime_s} {cpu_load} {mem_free_mb} {temperature_c}`
  (host metrics are Linux-only)
- **Timing:** `{timestamp} {sampled_at}`

**Heartbeats while cellular is down:** probes don't stop when the connection drops.
While cellular is **up**, the socket is bound to the cellular interface, so a success
proves cellular egress even with ethernet connected — and `{status}` renders as
`connected`. While cellular is **down** but the Pi still has another route
(ethernet/Wi-Fi), probes keep firing unbound over whatever works, with
`{status}` = `degraded` and `{status_message}` carrying a one-line reason
(e.g. `recovery in progress: connect failed: ...`, `modem found, waiting for SIM: no SIM
inserted`). During a fallback test `{status}` is `fallback_test` so you can suppress
alerts for intentional outages. Uncheck **keep sending while degraded** to restore
pause-until-reconnected behavior, and uncheck **bind to cellular** when your endpoint is
only reachable over the LAN/VPN (e.g. testing against a local server).

## 8. Test Hologram fallback / outage protection

On the dashboard, set a duration (default 900 s = 15 min) and click **Start fallback
test**. The daemon puts the modem in airplane mode, shows a countdown, then re-enables
the radio, re-reads the ICCID (the SIM applet may have switched profiles), re-matches
your profiles, and reconnects. The Timeline page logs the before/after identity.

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
6. Heartbeat arrives at your endpoint with the **cellular** source IP while ethernet is up,
   and the payload reports `status=connected`.
7. Unscrew the antenna / `sudo mmcli -m 0 --disable` with ethernet connected → heartbeats
   keep arriving (now via ethernet) with `status=degraded` and a sensible `status_message`.
8. `sudo mmcli -m 0 --command='AT+CGDCONT?'`… or check the Timeline: PDP contexts equal
   the profile exactly (extras deleted on connect).
9. 2-minute fallback test round-trips; Timeline shows before/after ICCID.
10. Hot-swap SIMs while running → new profile applied automatically.
11. `sudo mmcli -m 0 --disable` → supervisor recovers without a service restart.
12. `sudo kill -STOP $(pidof -x python | head -1)` (or the sim-monitor PID) → systemd
    watchdog restarts it once; no restart loop.
13. Reboot → reconnects unattended within ~1–2 min of boot.

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

## Not affiliated with Hologram

This is an independent, community project. It is **not** affiliated with,
endorsed by, or supported by Hologram — it simply works well with their SIMs.
"Hologram" appears only where naming the carrier/APN is technically necessary.

## License

MIT
