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

from sim_monitor.config.loader import load_profiles
from sim_monitor.config.matcher import match_profile
from sim_monitor.config.schema import AppConfig, Profile
from sim_monitor.core import commands as cmd
from sim_monitor.core.commands import CommandQueue
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import (
    DiagnosticEntry,
    DiagnosticsReport,
    FallbackStatus,
    StateStore,
)
from sim_monitor.core.states import State
from sim_monitor.core.supervisor import RecoveryAction, Supervisor
from sim_monitor.modem.driver_base import ModemDetector, ModemDriver, ModemError
from sim_monitor.modem.pdp_reconcile import DefineContext, DeleteContext, reconcile
from sim_monitor.system.backend import BackendError, NetworkBackend
from sim_monitor.system.sdnotify import SdNotifier

log = logging.getLogger(__name__)

S = State  # short alias for the state handlers


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

        store.update(profile_count=len(profiles))

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
                if self.driver:
                    try:
                        self.driver.full_reset()
                    except ModemError as e:
                        self.events.error("modem", f"reset failed: {e}")
                self._safe_disconnect()
                self._drop_modem()
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
            case cmd.ReloadProfiles():
                self._reload_profiles()

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
        self.store.update(vendor=identity.vendor, model=identity.model, imei=identity.imei)
        if not sim.present:
            self.store.update(sim_present=False, iccid=None, imsi=None, last_error=sim.detail)
            return  # keep polling: SIM may be inserted any moment
        self.store.update(sim_present=True, iccid=sim.iccid, imsi=sim.imsi)
        self._record_identity("sim-ready")
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
        self.active_profile = profile
        self._go(S.CONFIGURING, active_profile=profile.name, last_error=None)

    def _state_configuring(self) -> None:
        assert self.driver is not None and self.active_profile is not None
        profile = self.active_profile
        try:
            if profile.at_init:
                self.driver.run_init_commands(profile.at_init)
            actions = reconcile(self.driver.get_pdp_contexts(), profile.pdp_contexts)
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
            self.backend.configure_connection(profile)
        except (ModemError, BackendError) as e:
            self._fail(f"configuration failed: {e}")
            return
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
                self._fail(f"connect failed: {e}")
                return
            conn = self.backend.get_connection_state()
        if conn.active and conn.ip_address:
            self.events.info(
                "connection", f"connected: {conn.interface} {conn.ip_address}"
            )
            try:
                self.backend.assert_routing(self.active_profile)
            except BackendError as e:
                self.events.warning("routing", f"could not assert routing: {e}")
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
            self._fail("timed out waiting for network registration / IP address")

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
                sim_present=False, iccid=None, imsi=None,
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
            self._sim_refresh_pending = False
            self._safe_disconnect()
            self.active_profile = None
            self.store.update(iccid=sim.iccid, imsi=sim.imsi)
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
            self.events.warning("routing", "default route drifted; re-asserting metric")
            try:
                self.backend.assert_routing(self.active_profile)
                routing_ok = self.backend.verify_routing(self.active_profile)
            except BackendError as e:
                self.events.error("routing", f"failed to re-assert routing: {e}")

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
        )

    def _state_degraded(self) -> None:
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

    def _drop_modem(self) -> None:
        """Forget the modem and re-detect from scratch (after resets/power cycles)."""
        self.driver = None
        self.active_profile = None
        self._events_enabled = False
        self._sim_refresh_pending = False
        self._registration = None
        self._last_identity = None
        self._go(
            S.NO_MODEM,
            sim_present=False, iccid=None, imsi=None,
            interface=None, ip_address=None, active_profile=None,
            vendor=None, model=None, imei=None,
        )
