"""Drive the daemon state machine end-to-end against the fake modem/backend."""

import pytest
import yaml

from sim_monitor.config.schema import AppConfig, Profile
from sim_monitor.core import commands as cmd
from sim_monitor.core.commands import CommandQueue
from sim_monitor.core.daemon import Daemon
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.states import State
from sim_monitor.modem.fake import DEFAULT_ICCID, FALLBACK_ICCID, FakeDetector, FakeModemDriver
from sim_monitor.storage.db import Database
from sim_monitor.system.fake_backend import FakeBackend

DEFAULT_PROFILE = Profile.model_validate(
    {
        "name": "hologram-default",
        "match": {"iccid_patterns": ["*"], "priority": 1000},
        "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
    }
)

FALLBACK_PROFILE = Profile.model_validate(
    {
        "name": "fallback-sims",
        "match": {"iccid_patterns": ["8944999*"], "priority": 10},
        "pdp_contexts": [
            {"cid": 1, "apn": "hologram", "pdp_type": "IPv4v6", "bearer": True},
            {"cid": 2, "apn": "hologram.special"},
        ],
        "at_init": ['AT+QCFG="nwscanmode",0'],
    }
)


class Harness:
    def __init__(self, tmp_path, profiles=None, appear_after=0):
        self.t = 0.0
        self.driver = FakeModemDriver()
        self.detector = FakeDetector(self.driver, appear_after=appear_after)
        self.backend = FakeBackend(self.driver)
        self.store = StateStore()
        self.queue = CommandQueue()
        self.db = Database(":memory:")
        self.events = EventLog(self.db)
        self.profiles_dir = tmp_path / "profiles.d"
        self.profiles_dir.mkdir(exist_ok=True)
        config = AppConfig.model_validate(
            {"profiles_dir": str(self.profiles_dir), "daemon": {"connect_timeout_seconds": 30}}
        )
        self.daemon = Daemon(
            config=config,
            profiles=profiles if profiles is not None else [DEFAULT_PROFILE],
            detector=self.detector,
            backend=self.backend,
            store=self.store,
            command_queue=self.queue,
            events=self.events,
            clock=lambda: self.t,
        )

    def tick(self, n=1, advance=0.0):
        for _ in range(n):
            self.t += advance
            self.daemon.tick()

    def run_until(self, state: State, max_ticks=25, advance=0.0):
        for _ in range(max_ticks):
            self.tick(advance=advance)
            if self.daemon.state is state:
                return
        raise AssertionError(
            f"never reached {state.value}; stuck in {self.daemon.state.value}"
            f" (last_error={self.store.get().last_error})"
        )

    def event_kinds(self):
        return [e["kind"] for e in self.db.recent_events(500)]


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


class TestHappyPath:
    def test_walks_to_connected(self, harness):
        harness.run_until(State.CONNECTED)
        snap = harness.store.get()
        assert snap.iccid == DEFAULT_ICCID
        assert snap.active_profile == "hologram-default"
        assert snap.ip_address == "10.170.42.7"
        assert snap.interface == "wwan0"
        assert harness.backend.configured_profile.name == "hologram-default"

    def test_pdp_contexts_reconciled_exactly(self, harness):
        # Fake modem boots with stray contexts (cid 1 wrong APN, cid 8 extra).
        harness.run_until(State.CONNECTED)
        contexts = harness.driver.get_pdp_contexts()
        assert [(c.cid, c.apn) for c in contexts] == [(1, "hologram")]

    def test_modem_appearing_late(self, tmp_path):
        h = Harness(tmp_path, appear_after=3)
        h.tick(2)
        assert h.daemon.state is State.NO_MODEM
        h.run_until(State.CONNECTED)

    def test_sim_inserted_later(self, harness):
        harness.driver.sim_present = False
        harness.tick(5)
        assert harness.daemon.state is State.MODEM_FOUND
        assert harness.store.get().sim_present is False
        harness.driver.sim_present = True
        harness.run_until(State.CONNECTED)

    def test_connected_updates_signal_and_routing(self, harness):
        harness.run_until(State.CONNECTED)
        harness.tick()
        snap = harness.store.get()
        assert snap.operator == "Hologram"
        assert snap.signal_rssi == -77
        assert snap.routing_ok is True


class TestProfileSelection:
    def test_specific_profile_beats_default(self, tmp_path):
        h = Harness(tmp_path, profiles=[DEFAULT_PROFILE, FALLBACK_PROFILE])
        h.driver.iccid = "8944999900000000077"
        h.run_until(State.CONNECTED)
        assert h.store.get().active_profile == "fallback-sims"
        # at_init commands ran, both PDP contexts defined
        assert 'AT+QCFG="nwscanmode",0' in h.driver.at_log
        assert sorted(h.driver.contexts) == [1, 2]

    def test_force_profile_command(self, tmp_path):
        h = Harness(tmp_path, profiles=[DEFAULT_PROFILE, FALLBACK_PROFILE])
        h.run_until(State.CONNECTED)
        assert h.store.get().active_profile == "hologram-default"
        h.queue.put(cmd.ForceProfile("fallback-sims"))
        h.run_until(State.CONNECTED, max_ticks=10)
        snap = h.store.get()
        assert snap.active_profile == "fallback-sims"
        assert snap.forced_profile == "fallback-sims"
        h.queue.put(cmd.ReleaseForce())
        h.run_until(State.CONNECTED, max_ticks=10)
        assert h.store.get().active_profile == "hologram-default"

    def test_force_unknown_profile_rejected(self, harness):
        harness.run_until(State.CONNECTED)
        harness.queue.put(cmd.ForceProfile("nope"))
        harness.tick()
        assert harness.daemon.state is State.CONNECTED
        assert harness.store.get().forced_profile is None

    def test_reload_profiles_from_disk(self, harness):
        harness.run_until(State.CONNECTED)
        new_profile = {
            "name": "exact-match",
            "match": {"iccid_patterns": [DEFAULT_ICCID], "priority": 1},
            "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
        }
        (harness.profiles_dir / "10-exact.yaml").write_text(
            yaml.safe_dump(new_profile), encoding="utf-8"
        )
        harness.queue.put(cmd.ReloadProfiles())
        harness.run_until(State.CONNECTED, max_ticks=10)
        snap = harness.store.get()
        assert snap.profile_count == 1
        assert snap.active_profile == "exact-match"


class TestHotSwap:
    def test_iccid_change_rematches(self, tmp_path):
        h = Harness(tmp_path, profiles=[DEFAULT_PROFILE, FALLBACK_PROFILE])
        h.run_until(State.CONNECTED)
        assert h.store.get().active_profile == "hologram-default"
        h.driver.iccid = "8944999900000000077"  # swap the SIM
        h.tick()
        assert h.daemon.state is State.SIM_READY
        h.run_until(State.CONNECTED)
        assert h.store.get().active_profile == "fallback-sims"
        assert "sim" in h.event_kinds()

    def test_sim_removed_while_connected(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.sim_present = False
        harness.tick()
        assert harness.daemon.state is State.MODEM_FOUND
        snap = harness.store.get()
        assert snap.sim_present is False
        assert snap.ip_address is None


class TestRecovery:
    def test_connect_failure_walks_ladder(self, harness):
        harness.backend.fail_connect = True
        harness.run_until(State.DEGRADED)
        assert harness.daemon.supervisor.failures == 1
        # rung 1: reconnect after 10s backoff
        harness.tick(advance=11)  # due -> CONFIGURING
        harness.tick()  # CONFIGURING -> CONNECTING
        harness.tick()  # connect fails -> DEGRADED (failure 2)
        assert harness.daemon.supervisor.failures == 2
        # rung 2: modem disable/enable after 20s
        harness.tick(advance=21)
        assert harness.backend.disable_enable_calls == 1
        harness.tick(2)  # configure, connect fails -> failure 3
        assert harness.daemon.supervisor.failures == 3
        # rung 3: AT reset after 40s -> FPLMN cleared first, then NO_MODEM redetect
        harness.tick(advance=41)
        assert "CLEAR_FPLMN" in harness.driver.at_log
        assert harness.driver.at_log.index("CLEAR_FPLMN") < harness.driver.at_log.index("RESET")
        assert harness.daemon.state is State.NO_MODEM

    def test_recovers_when_failure_clears(self, harness):
        harness.backend.fail_connect = True
        harness.run_until(State.DEGRADED)
        harness.backend.fail_connect = False
        harness.tick(advance=11)  # recovery action: reconnect
        harness.run_until(State.CONNECTED, max_ticks=5)

    def test_connection_drop_triggers_recovery(self, harness):
        harness.run_until(State.CONNECTED)
        harness.backend.drop_connection = True
        harness.tick()
        assert harness.daemon.state is State.DEGRADED
        harness.tick(advance=11)
        harness.run_until(State.CONNECTED, max_ticks=5)

    def test_stable_connection_resets_supervisor(self, harness):
        harness.backend.fail_connect = True
        harness.run_until(State.DEGRADED)
        harness.backend.fail_connect = False
        harness.tick(advance=11)
        harness.run_until(State.CONNECTED, max_ticks=5)
        harness.tick(5, advance=200)  # 1000s of stable CONNECTED ticks
        assert harness.daemon.supervisor.failures == 0

    def test_connecting_waits_while_nm_is_activating(self, harness):
        """Slow network registration must NOT trigger retries/escalation:
        re-kicking activation cancels in-flight registration."""
        harness.backend.activation_ticks = 4
        harness.run_until(State.CONNECTING)
        connect_calls_state = harness.backend.connected
        harness.tick(2, advance=5)
        assert harness.daemon.state is State.CONNECTING  # patiently waiting
        assert harness.daemon.supervisor.failures == 0
        assert connect_calls_state is False
        harness.run_until(State.CONNECTED)

    def test_registration_deadline_eventually_fails(self, harness):
        harness.backend.connect_is_noop = True  # NM keeps accepting, nothing happens
        harness.run_until(State.CONNECTING)
        harness.tick(advance=301)  # past registration_timeout_seconds (300)
        assert harness.daemon.state is State.DEGRADED
        assert "registration" in harness.store.get().last_error

    def test_modem_failure_never_crashes_tick(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.fail_all = True
        harness.tick(5, advance=5)  # must not raise
        assert harness.daemon.state in (State.DEGRADED, State.NO_MODEM)


class TestFallbackTest:
    def test_full_fallback_cycle_with_profile_switch(self, tmp_path):
        h = Harness(tmp_path, profiles=[DEFAULT_PROFILE, FALLBACK_PROFILE])
        h.run_until(State.CONNECTED)
        h.driver.fallback_iccid = FALLBACK_ICCID  # applet switches while radio is off
        h.queue.put(cmd.StartFallbackTest(duration_seconds=60))
        h.tick()
        assert h.daemon.state is State.FALLBACK_TEST
        assert h.driver.airplane is True
        snap = h.store.get()
        assert snap.fallback.active
        assert snap.fallback.iccid_before == DEFAULT_ICCID
        h.tick(advance=30)
        assert h.daemon.state is State.FALLBACK_TEST  # still waiting
        h.tick(advance=31)  # window elapsed
        assert h.driver.airplane is False
        h.run_until(State.CONNECTED)
        snap = h.store.get()
        assert snap.iccid == FALLBACK_ICCID
        assert snap.active_profile == "fallback-sims"
        assert not snap.fallback.active

    def test_abort_fallback(self, harness):
        harness.run_until(State.CONNECTED)
        harness.queue.put(cmd.StartFallbackTest(duration_seconds=600))
        harness.tick()
        assert harness.daemon.state is State.FALLBACK_TEST
        harness.queue.put(cmd.AbortFallbackTest())
        harness.tick()
        assert harness.driver.airplane is False
        harness.run_until(State.CONNECTED)

    def test_fallback_rejected_when_not_connected(self, harness):
        harness.tick()  # still NO_MODEM/MODEM_FOUND
        harness.queue.put(cmd.StartFallbackTest(duration_seconds=60))
        harness.tick()
        assert harness.daemon.state is not State.FALLBACK_TEST


class TestCommands:
    def test_reset_modem(self, harness):
        harness.run_until(State.CONNECTED)
        harness.queue.put(cmd.ResetModem())
        harness.tick()
        # Command drops to NO_MODEM; the same tick's handler may already re-detect.
        assert harness.daemon.state in (State.NO_MODEM, State.MODEM_FOUND)
        assert "RESET" in harness.driver.at_log
        harness.run_until(State.CONNECTED)

    def test_manual_reconnect(self, harness):
        harness.run_until(State.CONNECTED)
        harness.queue.put(cmd.Reconnect())
        harness.tick()
        # Command moves to CONFIGURING; the same tick's handler advances to CONNECTING.
        assert harness.daemon.state in (State.CONFIGURING, State.CONNECTING)
        assert not harness.backend.connected or harness.daemon.state is State.CONNECTING
        harness.run_until(State.CONNECTED)

    def test_run_monitor_now_sets_trigger(self, harness):
        harness.run_until(State.CONNECTED)
        assert not harness.daemon.monitor_trigger.is_set()
        harness.queue.put(cmd.RunMonitorNow())
        harness.tick()
        assert harness.daemon.monitor_trigger.is_set()

    def test_diagnostics_standard_bundle(self, harness):
        harness.run_until(State.CONNECTED)
        harness.queue.put(cmd.RunDiagnostics())
        harness.tick()
        report = harness.store.get().diagnostics
        assert report is not None and report.note == ""
        commands = [e.command for e in report.entries]
        assert commands == harness.driver.DIAGNOSTIC_COMMANDS
        csq = next(e for e in report.entries if e.command == "AT+CSQ")
        assert csq.ok and csq.output == "+CSQ: 18,99"

    def test_diagnostics_custom_commands_and_errors(self, harness):
        harness.run_until(State.CONNECTED)
        harness.queue.put(cmd.RunDiagnostics(commands=("AT+CEREG?", "AT+CSQ")))
        harness.tick()
        report = harness.store.get().diagnostics
        assert [e.command for e in report.entries] == ["AT+CEREG?", "AT+CSQ"]
        # A wedged modem reports per-command errors, doesn't crash the tick.
        harness.driver.fail_all = True
        harness.queue.put(cmd.RunDiagnostics(commands=("AT+CSQ",)))
        harness.tick()
        entry = harness.store.get().diagnostics.entries[0]
        assert not entry.ok
        assert "simulated modem failure" in entry.output

    def test_diagnostics_without_modem(self, tmp_path):
        h = Harness(tmp_path, appear_after=99)
        h.tick()
        h.queue.put(cmd.RunDiagnostics())
        h.tick()
        report = h.store.get().diagnostics
        assert report.entries == ()
        assert "no modem" in report.note
