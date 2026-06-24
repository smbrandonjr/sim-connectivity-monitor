"""The connectivity daemon: a tick-based state machine.

One tick = pet the watchdog, drain commands, run the current state's handler,
publish a snapshot. Every hardware/system call is wrapped so failures become
supervisor-managed recovery, never crashes. This thread is the sole owner of
modem and network mutations.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from sim_monitor.config.loader import load_profiles
from sim_monitor.config.matcher import match_profile
from sim_monitor.config.schema import (
    AppConfig,
    MonitorConfig,
    Profile,
    SmsAutoReplyConfig,
)
from sim_monitor.core import commands as cmd
from sim_monitor.core.commands import CommandQueue
from sim_monitor.core.events import EventLog
from sim_monitor.core.sms_reply import find_reply
from sim_monitor.core.state_store import (
    DiagnosticEntry,
    DiagnosticsReport,
    FallbackStatus,
    ModemSetup,
    SerialPort,
    StateStore,
)
from sim_monitor.core.states import State
from sim_monitor.core.supervisor import RecoveryAction, Supervisor
from sim_monitor.modem.driver_base import (
    RAT_LABELS,
    ModemDetector,
    ModemDriver,
    ModemError,
)
from sim_monitor.modem.pdp_reconcile import DefineContext, DeleteContext, reconcile
from sim_monitor.system.backend import BackendError, NetworkBackend
from sim_monitor.system.sdnotify import SdNotifier

log = logging.getLogger(__name__)

S = State  # short alias for the state handlers

# Last-ditch catch-all used when the matched profile can't connect after the
# recovery ladder is exhausted (daemon.fallback_to_default_profile). A plain
# IPv4 "hologram" APN gets most Hologram SIMs online so a device is never
# stranded by a missing or wrong profile.
BUILTIN_DEFAULT = Profile.model_validate(
    {
        "name": "builtin-default",
        "description": "Built-in last-ditch default (APN hologram, IPv4)",
        "match": {"iccid_patterns": ["*"], "priority": 100000},
        "pdp_contexts": [{"cid": 1, "apn": "hologram", "pdp_type": "IPv4", "bearer": True}],
    }
)


def is_ppp_interface(name: str | None) -> bool:
    """True for legacy serial PPP data links (ppp0, ppp1, ...). On a PPP link
    the modem dialed up over serial instead of exposing a native wwan/QMI
    netdev, so there's usually no real gateway and cellular can't be made the
    lowest-metric default route."""
    return bool(name) and name.startswith("ppp")


class Daemon:
    def __init__(
        self,
        config: AppConfig,
        profiles: list[Profile],
        detector: ModemDetector,
        backend: NetworkBackend,
        store: StateStore,
        command_queue: CommandQueue,
        events: EventLog,
        db,
        clock=time.monotonic,
        notifier: SdNotifier | None = None,
    ) -> None:
        self.config = config
        self.profiles = profiles
        self.detector = detector
        self.backend = backend
        self.store = store
        self.commands = command_queue
        self.events = events
        self.db = db
        self.clock = clock
        self.notifier = notifier
        self.supervisor = Supervisor()
        self.monitor_trigger = threading.Event()  # RunMonitorNow -> monitor thread

        self.state = S.NO_MODEM
        self.driver: ModemDriver | None = None
        self.active_profile: Profile | None = None
        self.forced_profile: str | None = None
        self._connect_deadline: float | None = None
        self._activation_logged = False
        self._fallback_until: float | None = None
        self._events_enabled = False        # URC reporting configured on this driver
        self._sim_refresh_pending = False   # a SIM refresh/insert URC was seen
        self._sms_pending = False           # a +CMTI was seen (Phase 2 fetches)
        self._last_identity: tuple | None = None  # (iccid, imsi, imei) last recorded
        self._registration: str | None = None
        self._variant_index = 0             # which PDP-context variant we're trying
        self._fallback_active = False       # on the built-in last-ditch default
        self._next_telemetry = 0.0          # monotonic time of next telemetry poll
        self._next_routing_assert = 0.0     # throttle routing re-assertion
        self._routing_warned = False        # logged the current drift episode?
        self._next_sms_poll = 0.0           # monotonic time of next SMS backstop poll
        self._auto_reply_times: dict[str, list[float]] = {}  # peer -> recent reply times
        self._next_sim_reprobe = 0.0        # when to next nudge a SIM re-read
        self._ppp_reset_attempts = 0        # bounded modem resets to escape a PPP link
        self._next_port_scan = 0.0          # throttle serial-port scans while NO_MODEM
        self.global_monitor: MonitorConfig | None = None
        self._load_global_monitor()

        # Apply a UI-set AT-port override (survives restarts; overrides config.yaml).
        override = None
        try:
            override = self.db.get_setting("modem_at_port")
        except Exception:  # noqa: BLE001 - a bad stored value must not crash boot
            pass
        if override and hasattr(self.detector, "at_port"):
            self.detector.at_port = override

        store.update(
            profile_count=len(profiles),
            modem_setup=ModemSetup(at_port=getattr(self.detector, "at_port", "auto")),
        )

        # Connectivity uptime log: seed last-known state, then mark this start as
        # a down edge (any service-down gap counts as downtime; no-ops if we were
        # already down).
        last_conn = self.db.connectivity_last()
        self._conn_up: bool | None = bool(last_conn["up"]) if last_conn else None
        self._record_connectivity(False, S.NO_MODEM.value, "monitor started")

    def _load_global_monitor(self) -> None:
        """Load the global heartbeat config from the device DB (UI-managed)."""
        raw = self.db.get_setting("monitor")
        if not raw:
            self.global_monitor = None
            return
        try:
            self.global_monitor = MonitorConfig.model_validate(raw)
        except Exception as e:  # noqa: BLE001 - bad stored config must not crash boot
            log.warning("invalid stored global monitor config: %s", e)
            self.global_monitor = None

    def effective_monitor_config(self) -> MonitorConfig | None:
        """The heartbeat config to use now: a profile's monitor overrides the
        global one only when the profile explicitly enables it; otherwise the
        UI-managed global config applies."""
        profile = self.active_profile
        if profile and profile.monitor.enabled and profile.monitor.request is not None:
            return profile.monitor
        return self.global_monitor

    @property
    def sms_pending(self) -> bool:
        return self._sms_pending

    # ------------------------------------------------------------------ loop

    def run(self, stop: threading.Event) -> None:
        if self.notifier:
            self.notifier.ready()
        while not stop.is_set():
            self.tick()
            stop.wait(self.config.daemon.tick_seconds)

    def tick(self) -> None:
        if self.notifier:
            self.notifier.watchdog()
        try:
            self._poll_urcs()
            for command in self.commands.drain():
                self._handle_command(command)
            handler = getattr(self, f"_state_{self.state.value.lower()}")
            handler()
            self._maybe_fetch_sms()
        except Exception:
            # Last-resort guard: a bug must not kill the loop (systemd watchdog
            # still catches true hangs).
            log.exception("unhandled error in tick (state=%s)", self.state)
        self.store.update()

    def _go(self, state: State, **fields) -> None:
        if state != self.state:
            self.events.info("state", f"{self.state.value} -> {state.value}")
            self.state = state
        self.store.set_state(state, **fields)
        connected = state is S.CONNECTED
        self._record_connectivity(
            connected, state.value, None if connected else self.store.get().last_error
        )

    def _record_connectivity(self, connected: bool, state: str, detail: str | None) -> None:
        """Log a connectivity edge (connected<->down) once per change."""
        if self._conn_up is connected:
            return
        self._conn_up = connected
        try:
            self.db.add_connectivity(connected, state, detail)
        except Exception:  # noqa: BLE001 - an analytics write must never break the loop
            log.debug("connectivity log write failed", exc_info=True)

    def _fail(self, reason: str) -> None:
        planned = self.supervisor.on_failure(self.clock(), reason)
        wait = max(0, planned.not_before - self.clock())
        self.events.warning(
            "recovery",
            f"failure #{planned.attempt}: {reason}; next action"
            f" {planned.action.value} in {wait:.0f}s",
        )
        self._go(S.DEGRADED, last_error=reason)

    # ------------------------------------------------------------- commands

    def _handle_command(self, command) -> None:
        match command:
            case cmd.Reconnect():
                self.events.info("command", "manual reconnect requested")
                if self.active_profile and self.state in (
                    S.CONNECTED,
                    S.CONNECTING,
                    S.DEGRADED,
                ):
                    self._safe_disconnect()
                    self._go(S.CONFIGURING)
            case cmd.ResetModem():
                self.events.info("command", "manual modem reset requested")
                self._full_reset_modem()
            case cmd.ForceProfile(name=name):
                if not any(p.name == name for p in self.profiles):
                    self.events.error("profile", f"cannot force unknown profile {name!r}")
                    return
                self.forced_profile = name
                self.store.update(forced_profile=name)
                self.events.info("profile", f"profile {name!r} forced")
                self._reevaluate_profile()
            case cmd.ReleaseForce():
                self.forced_profile = None
                self.store.update(forced_profile=None)
                self.events.info("profile", "forced profile released; back to ICCID matching")
                self._reevaluate_profile()
            case cmd.StartFallbackTest(duration_seconds=duration):
                self._start_fallback_test(duration)
            case cmd.AbortFallbackTest():
                if self.state is S.FALLBACK_TEST:
                    self.events.info("fallback", "fallback test aborted")
                    self._end_fallback_test()
            case cmd.RunMonitorNow():
                self.monitor_trigger.set()
            case cmd.RunDiagnostics(commands=requested):
                self._run_diagnostics(requested)
            case cmd.PauseMonitor():
                self.store.update(monitor_paused=True)
                self.events.info("monitor", "heartbeats paused (manual sends still work)")
            case cmd.ResumeMonitor():
                self.store.update(monitor_paused=False)
                self.events.info("monitor", "heartbeats resumed")
            case cmd.SendSms(number=number, text=text):
                self._send_sms(number, text)
            case cmd.DeleteSms(row_id=row_id):
                self._delete_sms(row_id)
            case cmd.ClearSms():
                self._clear_sms()
            case cmd.RefreshSms():
                self._sms_pending = True
            case cmd.MarkSmsRead():
                self.db.mark_inbound_read()
                self.store.update(sms_unread=self.db.count_unread_sms())
            case cmd.SetSimName(name=name):
                self._set_sim_name(name)
            case cmd.ReloadMonitorConfig():
                self._load_global_monitor()
                self.events.info("monitor", "global heartbeat config updated")
            case cmd.ReloadProfiles():
                self._reload_profiles()
            case cmd.ScanSerialPorts():
                self._scan_serial_ports()
            case cmd.ProbeAtPort(device=device):
                self._probe_at_port(device)
            case cmd.SetAtPort(device=device):
                self._set_at_port(device)
            case cmd.SetRat(rat=rat):
                self._set_rat(rat)

    def _reevaluate_profile(self) -> None:
        """Re-run profile selection after a force/release/reload."""
        if self.state in (S.SIM_READY, S.CONFIGURING, S.CONNECTING, S.CONNECTED, S.DEGRADED):
            self._safe_disconnect()
            self._go(S.SIM_READY)

    def _reload_profiles(self) -> None:
        profiles, errors = load_profiles(self.config.profiles_dir)
        for err in errors:
            self.events.warning("profile", f"{err.path.name}: {err.error}")
        self.profiles = profiles
        self.store.update(profile_count=len(profiles))
        self.events.info("profile", f"profiles reloaded ({len(profiles)})")
        snapshot = self.store.get()
        if snapshot.iccid:
            selected = self._select_profile(snapshot.iccid)
            if selected is None or not self.active_profile or (
                selected.name != self.active_profile.name
            ):
                self._reevaluate_profile()

    # ------------------------------------------------------- state handlers

    def _state_no_modem(self) -> None:
        # Keep the modem-setup view fresh while we're stuck, so the UI can guide
        # the user to the right AT port without any manual refresh.
        if self.clock() >= self._next_port_scan:
            self._next_port_scan = self.clock() + 10
            self._scan_serial_ports()
        try:
            driver = self.detector.detect()
        except ModemError as e:
            self.store.update(last_error=str(e))
            return
        if driver is None:
            return
        self.driver = driver
        self.events.info("modem", f"modem detected ({driver.name})")
        self._enable_event_reporting()
        self._go(S.MODEM_FOUND)

    def _enable_event_reporting(self) -> None:
        """Turn on verbose URCs once per driver (best-effort)."""
        if self._events_enabled or self.driver is None:
            return
        try:
            self.driver.enable_event_reporting()
            self._events_enabled = True
        except ModemError as e:
            self.events.warning("modem", f"could not enable event reporting: {e}")

    def _poll_urcs(self) -> None:
        """Capture + react to unsolicited modem events each tick."""
        if self.driver is None:
            return
        try:
            urcs = self.driver.poll_events()
        except ModemError:
            return  # transient; the active state handler will detect a real wedge
        for ev in urcs:
            self.db.add_urc(ev.kind, ev.raw, ev.fields or None)
            if ev.kind == "sim_status":
                self._sim_refresh_pending = True
                self.events.info("ota", f"SIM status/refresh URC: {ev.raw}")
            elif ev.kind == "new_sms":
                self._sms_pending = True
                self.events.info("urc", f"new SMS indication: {ev.raw}")
            elif ev.kind == "registration":
                label = ev.fields.get("label", ev.raw)
                if label != self._registration:
                    self._registration = label
                    self.store.update(registration=label)
                    self.events.info("identity", f"registration: {label}")
                    self._record_identity("registration-change")
            elif ev.kind not in ("unknown",):
                self.events.info("urc", f"{ev.kind}: {ev.raw}")

    # ------------------------------------------------------------------ SMS

    _SMS_SAFE_STATES = (S.CONNECTED, S.MODEM_FOUND, S.SIM_READY, S.DEGRADED)
    SMS_POLL_SECONDS = 60       # backstop poll in case the +CMTI URC isn't captured
    SIM_REPROBE_SECONDS = 60    # how often to nudge a SIM re-read while none present
    # Loop guard: cap how many auto-replies a single peer can trigger in a
    # rolling window, so two auto-responders can't ping-pong forever.
    AUTO_REPLY_WINDOW_SECONDS = 3600
    AUTO_REPLY_MAX_PER_PEER = 5

    def _maybe_fetch_sms(self) -> None:
        if self.driver is None or self.state not in self._SMS_SAFE_STATES:
            return
        if not self.store.get().sim_present:
            return  # no SIM -> no SMS (and avoids spamming errors)
        # Periodic backstop: new SMS surface even if the +CMTI URC went to a
        # port we don't own.
        if self.clock() >= self._next_sms_poll:
            self._sms_pending = True
            self._next_sms_poll = self.clock() + self.SMS_POLL_SECONDS
        if not self._sms_pending:
            return
        self._sms_pending = False
        try:
            raw = self.driver.list_sms()
        except ModemError as e:
            self.events.warning("sms", f"could not list SMS: {e}")
            return
        from sim_monitor.modem.sms import reassemble_inbound

        new_rows = self.db.upsert_inbound_sms(reassemble_inbound(raw))
        self.store.update(sms_unread=self.db.count_unread_sms())
        if new_rows:
            self.events.info("sms", f"{len(new_rows)} new message(s) received")
            self._auto_reply(new_rows)

    def _send_sms(self, number: str, text: str) -> None:
        if self.driver is None:
            self.events.error("sms", "cannot send SMS: no modem")
            return
        try:
            parts = self.driver.send_sms(number, text)
        except ModemError as e:
            self.events.error("sms", f"send failed: {e}")
            return
        self.db.add_sent_sms(number, text, parts=parts)
        self.events.info("sms", f"sent {parts}-part message to {number}")

    def _auto_reply(self, new_rows: list[dict]) -> None:
        """For each genuinely-new inbound message, send the first matching
        auto-reply rule's response. Config is read fresh from the DB so UI edits
        take effect immediately; a per-peer rate cap prevents reply loops."""
        raw = self.db.get_setting("sms_auto_reply")
        if not raw:
            return
        try:
            config = SmsAutoReplyConfig.model_validate(raw)
        except Exception as e:  # noqa: BLE001 - bad stored config must not wedge SMS
            log.warning("invalid stored sms auto-reply config: %s", e)
            return
        if not config.enabled or not config.rules:
            return
        for row in new_rows:
            peer = (row.get("peer") or "").strip()
            if not peer:
                continue
            rule = find_reply(config, row.get("body") or "")
            if rule is None:
                continue
            if not self._auto_reply_allowed(peer):
                self.events.warning(
                    "sms",
                    f"auto-reply to {peer} suppressed (over "
                    f"{self.AUTO_REPLY_MAX_PER_PEER}/hr loop guard)",
                )
                continue
            label = rule.name or rule.pattern
            self.events.info("sms", f"auto-reply rule {label!r} matched SMS from {peer}")
            self._send_sms(peer, rule.reply)

    def _auto_reply_allowed(self, peer: str) -> bool:
        """Rate-limit auto-replies per peer (rolling window). Records the send
        when allowed."""
        now = self.clock()
        cutoff = now - self.AUTO_REPLY_WINDOW_SECONDS
        recent = [t for t in self._auto_reply_times.get(peer, []) if t >= cutoff]
        if len(recent) >= self.AUTO_REPLY_MAX_PER_PEER:
            self._auto_reply_times[peer] = recent
            return False
        recent.append(now)
        self._auto_reply_times[peer] = recent
        return True

    def _delete_sms(self, row_id: int) -> None:
        row = self.db.get_sms(row_id)
        if row is None:
            return
        if row["direction"] == "in" and self.driver is not None:
            for index in row["modem_indices"]:
                try:
                    self.driver.delete_sms(index)
                except ModemError as e:
                    self.events.warning("sms", f"delete index {index} failed: {e}")
            self._sms_pending = True  # re-sync inbound from the modem
        self.db.delete_sms_row(row_id)
        self.store.update(sms_unread=self.db.count_unread_sms())

    def _clear_sms(self) -> None:
        if self.driver is not None:
            try:
                self.driver.delete_all_sms()
            except ModemError as e:
                self.events.warning("sms", f"clear failed: {e}")
        self._sms_pending = True
        self.events.info("sms", "cleared all messages on the modem")

    def _log_ota_context(self, when: str) -> None:
        """Capture the observable network context around an OTA/swap. The OTA
        applet's SIM<->network exchange is below the AT interface, but operator,
        registration, signal, and serving cell give useful before/after detail."""
        if self.driver is None:
            return
        parts: list[str] = []
        try:
            op = self.driver.get_operator()
            if op:
                parts.append(f"operator={op}")
        except ModemError:
            pass
        try:
            t = self.driver.get_telemetry()
            for k in ("rat", "band", "rsrp", "rsrq", "sinr", "cell_id", "tac",
                      "operator_numeric"):
                if t.get(k) is not None:
                    parts.append(f"{k}={t[k]}")
        except ModemError:
            pass
        self.events.info(
            "ota", f"network context {when}: " + (", ".join(parts) or "unavailable")
        )

    def _set_sim_name(self, name: str) -> None:
        iccid = self.store.get().iccid
        if not iccid:
            self.events.warning("sim", "cannot name SIM: no SIM present")
            return
        self.db.set_sim_name(iccid, name.strip())
        self.store.update(sim_name=name.strip() or None)
        self.events.info("sim", f"SIM {iccid} named {name.strip()!r}" if name.strip()
                         else f"cleared name for SIM {iccid}")

    def _refresh_sim_name(self) -> None:
        """Resolve the stored name for the current ICCID into the snapshot."""
        self.store.update(sim_name=self.db.get_sim_name(self.store.get().iccid))

    def _record_identity(self, reason: str) -> None:
        snap = self.store.get()
        key = (snap.iccid, snap.imsi, snap.imei)
        if reason != "registration-change" and key == self._last_identity:
            return
        self._last_identity = key
        self.db.add_identity(
            iccid=snap.iccid, imsi=snap.imsi, imei=snap.imei,
            operator=snap.operator, registration=self._registration, reason=reason,
        )

    def _state_modem_found(self) -> None:
        assert self.driver is not None
        try:
            identity = self.driver.get_identity()
            sim = self.driver.get_sim_status()
        except ModemError as e:
            self.events.error("modem", f"modem stopped responding: {e}")
            self._drop_modem()
            return
        firmware = None
        try:
            firmware = self.driver.get_firmware()
        except ModemError:
            pass  # cosmetic; don't block bring-up
        self.store.update(
            vendor=identity.vendor, model=identity.model, imei=identity.imei,
            firmware=firmware, rat_supported=tuple(self.driver.supported_rats()),
        )
        if not sim.present:
            self.store.update(sim_present=False, iccid=None, imsi=None, last_error=sim.detail)
            # A SIM-status URC (insertion) makes us re-probe right away; otherwise
            # nudge periodically, since many modems don't auto-detect a hot swap.
            if self._sim_refresh_pending:
                self._sim_refresh_pending = False
                self._next_sim_reprobe = 0
            if self.clock() >= self._next_sim_reprobe:
                self._next_sim_reprobe = self.clock() + self.SIM_REPROBE_SECONDS
                try:
                    self.driver.reprobe_sim()
                    self.events.info("sim", "no SIM detected; re-probing modem for insertion")
                except ModemError as e:
                    self.events.warning("sim", f"SIM re-probe failed: {e}")
            return  # keep polling: SIM may be inserted any moment
        self._next_sim_reprobe = 0  # reset so a future removal re-probes promptly
        self.store.update(sim_present=True, iccid=sim.iccid, imsi=sim.imsi)
        self._refresh_sim_name()
        self._record_identity("sim-ready")
        self._sms_pending = True  # sync the inbox once the SIM is up
        self.events.info("sim", f"SIM ready, ICCID {sim.iccid}")
        self._go(S.SIM_READY)

    def _select_profile(self, iccid: str) -> Profile | None:
        if self.forced_profile:
            return next((p for p in self.profiles if p.name == self.forced_profile), None)
        result = match_profile(iccid, self.profiles)
        return result.profile if result else None

    def _state_sim_ready(self) -> None:
        snapshot = self.store.get()
        if not snapshot.iccid:
            self._go(S.MODEM_FOUND)
            return
        profile = self._select_profile(snapshot.iccid)
        if profile is None:
            self.store.update(
                last_error=f"no profile matches ICCID {snapshot.iccid}", active_profile=None
            )
            return
        if self.active_profile is None or self.active_profile.name != profile.name:
            self.events.info(
                "profile",
                f"profile {profile.name!r} selected for ICCID {snapshot.iccid}"
                + (" (forced)" if self.forced_profile else ""),
            )
            self._variant_index = 0       # fresh profile: start at its first variant
            self._fallback_active = False
        self.active_profile = profile
        self._go(S.CONFIGURING, active_profile=profile.name, last_error=None)

    def _state_configuring(self) -> None:
        assert self.driver is not None and self.active_profile is not None
        profile = self.active_profile
        sets = profile.context_sets()
        if self._variant_index >= len(sets):
            self._variant_index = 0
        variant_label, contexts = sets[self._variant_index]
        bearer = next(c for c in contexts if c.bearer)
        if len(sets) > 1:
            self.events.info(
                "pdp", f"using PDP variant '{variant_label}' ({bearer.apn})"
            )
        try:
            if profile.at_init:
                self.driver.run_init_commands(profile.at_init)
            actions = reconcile(self.driver.get_pdp_contexts(), contexts)
            for action in actions:
                match action:
                    case DeleteContext(cid=cid):
                        self.driver.delete_pdp_context(cid)
                        self.events.info("pdp", f"deleted stray PDP context cid={cid}")
                    case DefineContext(context=ctx):
                        self.driver.define_pdp_context(ctx)
                        self.events.info(
                            "pdp", f"defined PDP context cid={ctx.cid} apn={ctx.apn}"
                        )
            self.backend.configure_connection(profile, bearer)
        except (ModemError, BackendError) as e:
            self._fail(f"configuration failed: {e}")
            return
        self.store.update(apn=bearer.apn)
        self._connect_deadline = (
            self.clock() + self.config.daemon.registration_timeout_seconds
        )
        self._activation_logged = False
        self._go(S.CONNECTING)

    def _state_connecting(self) -> None:
        """Patiently drive NM activation.

        Registration on roaming SIMs can take minutes (carrier scans, reject/
        retry cycles). NM reports `activating` the whole time; re-running
        `connection up` during that window CANCELS registration, so we only
        (re)kick activation when NM is idle, and otherwise just wait until
        the registration deadline.
        """
        conn = self.backend.get_connection_state()
        if not (conn.active and conn.ip_address) and not conn.activating:
            # Idle: first attempt, or NM gave up on the previous one -> re-kick.
            try:
                self.backend.connect()
            except BackendError as e:
                self._advance_variant_or_fail(f"connect failed: {e}")
                return
            conn = self.backend.get_connection_state()
        if conn.active and conn.ip_address:
            if not self._handle_ppp_link(conn.interface):
                return  # reset the modem to escape the PPP link; re-detect next tick
            self.events.info(
                "connection", f"connected: {conn.interface} {conn.ip_address}"
            )
            try:
                self.backend.assert_routing(self.active_profile)
            except BackendError as e:
                self.events.warning("routing", f"could not assert routing: {e}")
            self._record_identity("connected")
            self._go(
                S.CONNECTED,
                interface=conn.interface,
                ip_address=conn.ip_address,
                last_error=None,
            )
            return
        if conn.activating and not self._activation_logged:
            self._activation_logged = True
            self.events.info(
                "connection",
                "activation in progress (network registration); waiting up to "
                f"{self.config.daemon.registration_timeout_seconds}s",
            )
        if self._connect_deadline and self.clock() > self._connect_deadline:
            self._advance_variant_or_fail(
                "timed out waiting for network registration / IP address"
            )

    def _advance_variant_or_fail(self, reason: str) -> None:
        """On a connect/registration failure, try the profile's next PDP variant
        before escalating to the supervisor ladder. Variants are cheap to try and
        often the difference between a working and non-working data plan."""
        sets = self.active_profile.context_sets() if self.active_profile else [("", [])]
        if self._variant_index + 1 < len(sets):
            self._variant_index += 1
            label = sets[self._variant_index][0]
            self.events.info(
                "pdp", f"{reason}; trying next PDP variant '{label}'"
            )
            self._safe_disconnect()
            self._go(S.CONFIGURING)
        else:
            self._variant_index = 0  # next attempt starts from the top
            self._fail(reason)

    def _handle_ppp_link(self, interface: str | None) -> bool:
        """Decide whether a freshly-active connection is usable as-is.

        Returns True to proceed to CONNECTED. On a legacy PPP link (ppp0) the
        modem has no native wwan/QMI netdev, so the gateway is typically 0.0.0.0
        and cellular can't win the default route. When enabled, we reset the
        modem to coax it back to native mode and return False (the caller backs
        out; re-detection happens next tick). The number of resets is bounded so
        a modem that only ever does PPP isn't reset-looped forever -- past the
        cap we accept the PPP link to stay online."""
        if not is_ppp_interface(interface):
            self._ppp_reset_attempts = 0  # healthy native link: re-arm for regressions
            return True
        cap = self.config.daemon.ppp_reset_max_attempts
        if self.config.daemon.reset_on_ppp_interface and self._ppp_reset_attempts < cap:
            self._ppp_reset_attempts += 1
            self.events.warning(
                "connection",
                f"modem came up on legacy PPP link {interface} (no native "
                "wwan/QMI netdev; gateway/default-route unreliable); resetting "
                f"modem to recover native mode (attempt {self._ppp_reset_attempts}/{cap})",
            )
            self._full_reset_modem()
            return False
        if self._ppp_reset_attempts:  # resets were tried and didn't help
            self.events.warning(
                "connection",
                f"still on PPP link {interface} after {self._ppp_reset_attempts} "
                "reset attempt(s); accepting it to stay online (native wwan/QMI "
                "may need manual intervention)",
            )
        return True

    def _state_connected(self) -> None:
        assert self.driver is not None and self.active_profile is not None
        try:
            sim = self.driver.get_sim_status()
        except ModemError as e:
            self._fail(f"modem stopped responding: {e}")
            return

        snapshot = self.store.get()
        if not sim.present:
            self.events.warning("sim", "SIM removed while connected")
            self._safe_disconnect()
            self.active_profile = None
            self._sim_refresh_pending = False
            self._go(
                S.MODEM_FOUND,
                sim_present=False, iccid=None, imsi=None, sim_name=None,
                interface=None, ip_address=None, active_profile=None,
            )
            return
        # Detect a SIM/profile change three ways: a changed ICCID, a changed
        # IMSI (a profile swap can keep the ICCID but move the IMSI), or a SIM
        # refresh URC. A Hologram OTA swap often keeps the bearer superficially
        # "active" on the same APN, so connectivity alone never reveals it — and
        # the AT port may report the *stale* ICCID until a re-attach. Reacting to
        # any of the three is what makes the OTA reconnect that v1 missed.
        identity_changed = sim.iccid != snapshot.iccid or sim.imsi != snapshot.imsi
        if identity_changed or self._sim_refresh_pending:
            reason = (
                "SIM refresh / OTA profile change"
                if self._sim_refresh_pending and not identity_changed
                else "SIM identity change"
            )
            self.events.warning(
                "ota",
                f"{reason} while connected: "
                f"ICCID {snapshot.iccid}->{sim.iccid}, IMSI {snapshot.imsi}->{sim.imsi}; "
                "re-evaluating profile and re-attaching",
            )
            self._log_ota_context("at swap")
            self._sim_refresh_pending = False
            self._safe_disconnect()
            self.active_profile = None
            self.store.update(iccid=sim.iccid, imsi=sim.imsi)
            self._refresh_sim_name()
            self._record_identity("ota-swap")
            self._go(
                S.SIM_READY,
                iccid=sim.iccid, imsi=sim.imsi,
                interface=None, ip_address=None, active_profile=None,
            )
            return

        conn = self.backend.get_connection_state()
        if not conn.active:
            self._fail("connection lost")
            return

        routing_ok = self.backend.verify_routing(self.active_profile)
        if not routing_ok:
            # Re-assert at most every 30s (not every tick) and log once per
            # episode — otherwise a PPP link, whose metric we can't always pin,
            # spams the event log.
            if self.clock() >= self._next_routing_assert:
                self._next_routing_assert = self.clock() + 30
                try:
                    self.backend.assert_routing(self.active_profile)
                    routing_ok = self.backend.verify_routing(self.active_profile)
                except BackendError as e:
                    self.events.error("routing", f"failed to re-assert routing: {e}")
                if not routing_ok and not self._routing_warned:
                    self._routing_warned = True
                    self.events.warning(
                        "routing",
                        "cellular is not the lowest-metric default route; re-asserting "
                        "(can be normal briefly, or on a PPP link)",
                    )
        if routing_ok:
            self._routing_warned = False

        operator = signal = None
        try:
            operator = self.driver.get_operator()
            signal = self.driver.get_signal()
        except ModemError:
            pass  # cosmetic data; don't degrade over it
        self.supervisor.on_connected(self.clock())
        self.store.update(
            routing_ok=routing_ok,
            operator=operator,
            signal_rssi=signal.rssi_dbm if signal else None,
            signal_percent=signal.percent if signal else None,
            interface=conn.interface,
            ip_address=conn.ip_address,
            gateway=conn.gateway,
        )
        self._maybe_poll_telemetry()

    def _maybe_poll_telemetry(self) -> None:
        """Capture deep link metrics on an interval (cheap AT reads, charted)."""
        if self.driver is None or self.clock() < self._next_telemetry:
            return
        self._next_telemetry = self.clock() + self.config.daemon.telemetry_interval_seconds
        try:
            sample = self.driver.get_telemetry()
        except ModemError:
            return  # best-effort; cosmetic data
        if sample:
            self.store.update(telemetry=sample)
            self.db.add_telemetry(sample)

    def _state_degraded(self) -> None:
        # Last-ditch: once the ladder is exhausted on the matched profile, fall
        # back to the built-in default before parking forever (the user's
        # "catch-all try"). Only switch once, and never if already on it.
        if (
            self.config.daemon.fallback_to_default_profile
            and self.supervisor.parked
            and not self._fallback_active
            and self.active_profile is not None
            and self.active_profile.name != BUILTIN_DEFAULT.name
        ):
            self.events.warning(
                "profile",
                f"profile {self.active_profile.name!r} exhausted recovery; "
                "falling back to built-in default (APN hologram)",
            )
            self._fallback_active = True
            self.active_profile = BUILTIN_DEFAULT
            self._variant_index = 0
            self.supervisor.reset()
            self.store.update(active_profile=BUILTIN_DEFAULT.name)
            self._go(S.CONFIGURING)
            return
        planned = self.supervisor.due(self.clock())
        if planned is None:
            return
        self.supervisor.consume()
        self.events.info(
            "recovery", f"executing recovery action: {planned.action.value}"
        )
        try:
            match planned.action:
                case RecoveryAction.RECONNECT:
                    self._go(S.CONFIGURING)
                case RecoveryAction.MODEM_DISABLE_ENABLE:
                    self.backend.modem_disable_enable()
                    self._go(S.CONFIGURING)
                case RecoveryAction.AT_RESET:
                    if self.driver:
                        try:
                            # Persistent registration failure may mean in-plan
                            # carriers got onto the SIM's forbidden list.
                            self.driver.clear_forbidden_plmn()
                            self.events.info(
                                "recovery", "cleared SIM forbidden-PLMN list"
                            )
                        except ModemError as e:
                            self.events.warning(
                                "recovery", f"FPLMN clear failed: {e}"
                            )
                        self.driver.full_reset()
                    self._drop_modem()
                case RecoveryAction.USB_POWER_CYCLE:
                    self.backend.usb_power_cycle()
                    self._drop_modem()
        except (ModemError, BackendError) as e:
            self._fail(f"recovery action {planned.action.value} failed: {e}")

    def _state_fallback_test(self) -> None:
        assert self._fallback_until is not None
        if self.clock() < self._fallback_until:
            return
        self.events.info("fallback", "airplane-mode window elapsed; re-enabling radio")
        self._end_fallback_test()

    def _run_diagnostics(self, requested: tuple[str, ...]) -> None:
        """Run AT diagnostics over the dedicated port (UI Diagnostics page)."""
        if self.driver is None:
            self.store.update(
                diagnostics=DiagnosticsReport(
                    ran_at=time.time(), note="no modem detected; cannot run AT commands"
                )
            )
            return
        commands = list(requested) or self.driver.DIAGNOSTIC_COMMANDS
        entries = []
        for command in commands[:20]:  # bound the tick's serial time
            if self.notifier:
                self.notifier.watchdog()  # long scans must not trip WatchdogSec
            # Network scans legitimately take minutes; everything else is fast.
            timeout = 100 if command.upper().startswith("AT+COPS=?") else 10
            try:
                lines = self.driver.execute_raw(command, timeout=timeout)
                entries.append(
                    DiagnosticEntry(command, "\n".join(lines) or "OK", ok=True)
                )
            except ModemError as e:
                entries.append(DiagnosticEntry(command, str(e), ok=False))
        self.store.update(
            diagnostics=DiagnosticsReport(ran_at=time.time(), entries=tuple(entries))
        )
        self.events.info("diagnostics", f"ran {len(entries)} diagnostic command(s)")

    # ------------------------------------------------------------- fallback

    def _start_fallback_test(self, duration: int | None) -> None:
        if self.driver is None or self.state not in (S.CONNECTED, S.DEGRADED):
            self.events.error(
                "fallback", f"cannot start fallback test in state {self.state.value}"
            )
            return
        profile = self.active_profile
        seconds = duration or (profile.fallback_test.airplane_seconds if profile else 900)
        snapshot = self.store.get()
        try:
            self.driver.set_airplane(True)
        except ModemError as e:
            self.events.error("fallback", f"failed to enter airplane mode: {e}")
            return
        self._safe_disconnect()
        self._fallback_until = self.clock() + seconds
        self.events.info(
            "fallback",
            f"fallback test started: airplane mode for {seconds}s"
            f" (ICCID before: {snapshot.iccid})",
        )
        self._go(
            S.FALLBACK_TEST,
            interface=None,
            ip_address=None,
            fallback=FallbackStatus(
                active=True, until=time.time() + seconds, iccid_before=snapshot.iccid
            ),
        )

    def _end_fallback_test(self) -> None:
        assert self.driver is not None
        self._fallback_until = None
        try:
            self.driver.set_airplane(False)
            sim = self.driver.get_sim_status()
        except ModemError as e:
            self.store.update(fallback=FallbackStatus())
            self._fail(f"failed to exit airplane mode: {e}")
            return
        before = self.store.get().fallback.iccid_before
        if sim.present and sim.iccid != before:
            self.events.info(
                "fallback", f"SIM profile switched: {before} -> {sim.iccid}"
            )
        else:
            self.events.info("fallback", f"SIM profile unchanged ({sim.iccid})")
        self.active_profile = None
        self._go(
            S.SIM_READY if sim.present else S.MODEM_FOUND,
            sim_present=sim.present,
            iccid=sim.iccid,
            imsi=sim.imsi,
            active_profile=None,
            fallback=FallbackStatus(),
        )

    # -------------------------------------------------------------- helpers

    def _safe_disconnect(self) -> None:
        try:
            self.backend.disconnect()
        except BackendError as e:
            self.events.warning("connection", f"disconnect failed: {e}")

    # ----------------------------------------------------- modem / AT-port setup

    def _current_at_port(self) -> str | None:
        """The serial device sim-monitor is using (resolved 'auto' if connected)."""
        resolved = getattr(self.detector, "last_at_port", None)
        if resolved:
            return resolved
        configured = getattr(self.detector, "at_port", "auto")
        return configured if configured and configured != "auto" else None

    def _scan_serial_ports(self) -> None:
        """Enumerate serial ports for the UI's modem-setup view (read-only)."""
        if not hasattr(self.detector, "scan_ports"):
            return
        try:
            present, scanned = self.detector.scan_ports(self._current_at_port())
        except Exception as e:  # noqa: BLE001 - never let a scan break the tick
            self.events.warning("modem", f"serial-port scan failed: {e}")
            return
        prior = {p.device: p for p in self.store.get().modem_setup.ports}
        ports = []
        for sp in scanned:
            old = prior.get(sp.device)
            ports.append(SerialPort(
                device=sp.device, vid=sp.vid, pid=sp.pid, interface=sp.interface,
                mm_claimed=sp.mm_claimed, is_current=sp.is_current,
                tested=old.tested if old else False,
                responded=old.responded if old else False,
                identity=old.identity if old else None,
                detail=old.detail if old else None,
            ))
        self.store.update(modem_setup=ModemSetup(
            at_port=getattr(self.detector, "at_port", "auto"),
            modem_present=present, scanned_at=time.time(), ports=tuple(ports),
        ))

    def _probe_at_port(self, device: str) -> None:
        """Open one serial port and ask the modem to identify itself, so the user
        can tell which port is the AT port. Runs on the daemon thread (sole serial
        owner). If we already hold this port, report the live identity instead of
        fighting over the handle."""
        if not hasattr(self.detector, "probe"):
            return
        current = self._current_at_port()
        if self.driver is not None and current and device == current:
            snap = self.store.get()
            responded = True
            identity = (f"{snap.vendor or ''} {snap.model or ''}".strip()
                        or "in use (responding)")
            detail = None
        else:
            responded, identity, detail = self.detector.probe(device)
        setup = self.store.get().modem_setup
        ports = tuple(
            replace(p, tested=True, responded=responded, identity=identity, detail=detail)
            if p.device == device else p
            for p in setup.ports
        )
        self.store.update(modem_setup=replace(setup, ports=ports))
        self.events.info(
            "modem",
            f"AT probe {device}: {'ok — ' + (identity or '') if responded else 'no response'}",
        )

    def _set_at_port(self, device: str) -> None:
        """Persist the chosen AT port (or 'auto') and re-detect with it."""
        auto = device.strip() in ("", "auto")
        value = "auto" if auto else device.strip()
        if hasattr(self.detector, "at_port"):
            self.detector.at_port = value
        try:
            self.db.set_setting("modem_at_port", None if auto else value)
        except Exception as e:  # noqa: BLE001
            self.events.warning("modem", f"could not persist AT port: {e}")
        self.events.info(
            "modem", "AT port set to auto-detect" if auto else f"AT port set to {value}"
        )
        self._safe_disconnect()
        self._drop_modem()       # re-detect with the new port on the next tick
        self._next_port_scan = 0  # rescan promptly so the UI shows the new 'current'

    def _set_rat(self, rat: str) -> None:
        """Force the modem's radio access technology, then re-attach on it."""
        if self.driver is None:
            self.events.error("rat", "cannot set network mode: no modem")
            return
        label = RAT_LABELS.get(rat, rat)
        try:
            self.driver.set_rat(rat)
        except ModemError as e:
            self.events.error("rat", f"could not set network mode to {label}: {e}")
            return
        self.events.info("rat", f"network mode set to {label}; re-attaching")
        # A RAT change deregisters the modem; proactively reconnect on the new
        # mode rather than waiting for the connection to be noticed as lost.
        if self.active_profile is not None and self.state in (
            S.CONNECTED, S.CONNECTING, S.DEGRADED,
        ):
            self._safe_disconnect()
            self._variant_index = 0
            self._go(S.CONFIGURING)

    def _full_reset_modem(self) -> None:
        """Reset the modem and re-detect from scratch: the USB device
        re-enumerates and ModemManager re-probes. Shared by the manual Reset
        command and the PPP-recovery path."""
        if self.driver:
            try:
                self.driver.full_reset()
            except ModemError as e:
                self.events.error("modem", f"reset failed: {e}")
        self._safe_disconnect()
        self._drop_modem()

    def _drop_modem(self) -> None:
        """Forget the modem and re-detect from scratch (after resets/power cycles)."""
        self.driver = None
        self.active_profile = None
        self._events_enabled = False
        self._sim_refresh_pending = False
        self._registration = None
        self._last_identity = None
        self._variant_index = 0
        self._fallback_active = False
        self._go(
            S.NO_MODEM,
            sim_present=False, iccid=None, imsi=None, sim_name=None,
            interface=None, ip_address=None, gateway=None, public_ip=None,
            active_profile=None, vendor=None, model=None, imei=None,
            rat_supported=(),
        )
