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
- Serve a **LAN web UI** for live status, profile management, SMS (inbox/send/auto-reply),
  continuous **per-interface latency (ICMP) and web-check (HTTP) charts**, on-device
  network tools (host/port scan, reachability, traceroute), and manual actions
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

- **Dashboard** — device & cellular **uptime** (live, plus a searchable connectivity
  history with uptime %, outage count/duration, and a timeline over any timeframe);
  live state, ICCID/IMEI/operator/signal, IP / gateway / public IP,
  default-route check, serving-cell details (RAT/band/cell ID), and signal telemetry
  charts (RSRP/RSRQ/SINR/RSSI) that are colour-coded by quality, with hover tooltips
  explaining each metric and its good/fair/poor ranges; plus action buttons (reconnect,
  reset modem, run monitor probe, fallback test).
- **Profiles** — view/create/edit/delete profiles, force one for testing.
- **Messages** — SMS inbox and compose/send over the modem, plus **auto-reply rules**
  (define pattern → reply rules; the device texts a configured response back whenever an
  inbound SMS matches).
- **Monitoring** — heartbeat configuration, an optional schedule window, and recent
  heartbeat history.
- **Latency** — two continuous per-interface monitors: **ping** (ICMP latency &
  packet loss) and **web checks** (HTTP endpoint reachability with status codes). Each
  has interactive charts (one colour-coded line per interface, drag-to-zoom, hover
  tooltips), preset/custom timeframes, a per-interface/target summary table, and CSV
  export. Settings (targets, interval, timeout, retention, colours) are editable inline
  and hot-reload on the next cycle.
- **Scan** — network tools (host discovery, port scan, reachability checks,
  traceroute), optionally bound to a specific interface.
- **Timeline** — everything the daemon did (events, URCs, identity changes).
- **Diagnostics** — guided modem / AT-port setup (test & pick the right serial port for a
  new modem model), forcing the radio access technology (5G SA/NSA, LTE, LTE-M, NB-IoT,
  3G, 2G, or automatic), an AT command reference, and ad-hoc AT command execution.

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
  IPs `{eth0_ip} {wlan0_ip} {wwan0_ip}` (only for interfaces that are up), and
  `{egress_interface}` — the interface this heartbeat actually bound to (empty when
  OS-routed), so the endpoint can record which path each heartbeat took
- **Latency / ping (cellular path, from the ping monitor):** for the most recent cycle
  `{latency_ms} {latency_min_ms} {latency_max_ms} {loss_pct}`, and for each trailing
  window `w` in `1h/3h/6h/24h`: `{latency_<w>} {latency_min_<w>} {latency_max_<w>}
  {loss_<w>}` (e.g. `{latency_24h} {latency_max_24h} {loss_24h}`)
- **Web checks (cellular path, from the HTTP monitor):** the same set with an `http_`
  prefix — `{http_latency_ms} {http_latency_min_ms} {http_latency_max_ms} {http_loss_pct}`
  and per-window `{http_latency_<w>} {http_latency_min_<w>} {http_latency_max_<w>}
  {http_loss_<w>}`
- (both latency/web sets are empty until their monitor has data; they survive a brief
  disconnect via the last-known cellular interface. Pick exactly which ones to send in
  the **Monitoring → Payload** builder, under the *latency* and *web* groups.)
- **Host:** `{hostname} {uptime_s} {cpu_load} {mem_free_mb} {temperature_c}`
  (host metrics are Linux-only)
- **Timing:** `{timestamp} {sampled_at}`

**Egress interface (send over):** choose which interface each heartbeat goes out:
- **Wi-Fi (wlan)** — *default*. Heartbeats travel over Wi-Fi, keeping them off your
  cellular data — while `{status}` still reports cellular health (the daemon verifies the
  cellular link independently), so you can monitor cellular but deliver the report over
  Wi-Fi.
- **Cellular (wwan)** — bind to the modem interface, so a successful send *proves*
  cellular egress even with ethernet/Wi-Fi connected.
- **Any / OS default** — let the OS route it; use when the endpoint is only reachable over
  LAN/VPN (e.g. a local test server).

If the chosen interface is down, the probe falls back to OS routing rather than dropping
the heartbeat.

**Heartbeats while cellular is down:** probes don't stop when the connection drops. While
cellular is **up**, `{status}` renders as `connected`. While cellular is **down** but the
Pi still has another route, probes keep firing with `{status}` = `degraded` and
`{status_message}` carrying a one-line reason (e.g. `recovery in progress: connect
failed: ...`, `modem found, waiting for SIM: no SIM inserted`). During a fallback test
`{status}` is `fallback_test` so you can suppress alerts for intentional outages. Uncheck
**keep sending while degraded** to restore pause-until-reconnected behavior.

**Schedule (optional):** limit scheduled heartbeats to a weekly window — pick the days,
a start/end time, and a timezone (default **Mon–Fri, 9am–6pm, America/New_York**). It's
off by default (send around the clock). A manual **override** can force sending on or off
regardless of the window, and **Send heartbeat now** always fires. A badge shows whether
the monitor is sending right now.

## 8. Latency & web-check monitoring

The **Latency** tab hosts **two independent monitors**, each probing from *every* up
interface — cellular plus any ethernet/Wi-Fi — so it's obvious whether a problem is
cellular-only or systemic. Each keeps raw per-cycle samples short-term and folds them into
hourly/daily rollups for long-term history. Both are **off by default**.

- **Latency & packet loss (ping)** — ICMP pings a set of IP/host targets (public DNS
  anycast IPs by default), charting round-trip time and packet loss.
- **Web checks (HTTP)** — GETs a set of `http(s)://` URLs (e.g.
  `https://google.com/generate_204`), bound to each interface and timed, recording the
  **HTTP status code** and request latency; a status `< 400` is a success. This proves
  real end-to-end web reachability even when a carrier blocks or deprioritizes ICMP. The
  summary table and CSV include a status column.

Each panel has its own settings (gear icon): enable, targets, interval, timeout, retention,
and per-interface chart colours (the ping panel also has pings-per-target). Changes
hot-reload on the next cycle (no restart). Charts support preset (1h–30d) and custom
timeframes, drag-to-zoom, and hover tooltips; a summary table and CSV export cover the
selected window.

Each monitor exposes the cellular path's recent stats as heartbeat placeholders: ICMP as
`{latency_ms}` / `{loss_pct}` (+ `{latency_1h…24h}` / `{loss_1h…24h}`) and the web checks
as `{http_latency_ms}` / `{http_loss_pct}` (+ `{http_latency_1h…24h}` / `{http_loss_1h…24h}`).

Defaults also live in `config.yaml`'s `latency:` and `http_checks:` blocks; the in-UI
settings override them and persist on the device.

## 9. Test Hologram fallback / outage protection

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
| Dashboard stuck in `NO_MODEM`, events say "no AT port was found" | Your modem model isn't in the auto-detect hints. **Easiest:** the Dashboard shows a **Modem & AT port** panel (also under **Diagnostics**) — click **Test** on each port and **Use** the one that replies with the modem's name and isn't marked "MM uses". The choice is saved on the device. (Manual equivalents still work: set `modem.at_port` in `/etc/sim-monitor/config.yaml`, or add a udev rule to `/etc/udev/rules.d/77-sim-monitor.rules`.) |
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
13. Enable the Latency tab's **ping** and **web checks** monitors → within a couple of
    cycles each chart shows a line per up interface (cellular + ethernet/Wi-Fi), the web
    summary lists HTTP status codes, and CSV export downloads the window.
14. Reboot → reconnects unattended within ~1–2 min of boot.

---

## Development

Everything except real hardware runs anywhere (including Windows) via simulate mode.
Requires **Python 3.11+**:

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
