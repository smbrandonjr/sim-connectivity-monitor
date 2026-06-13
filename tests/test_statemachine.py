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
            db=self.db,
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

    def test_telemetry_captured_and_stored(self, harness):
        harness.run_until(State.CONNECTED)
        harness.tick(advance=60)  # past telemetry interval
        snap = harness.store.get()
        assert snap.telemetry.get("rsrp") == -94
        assert snap.telemetry.get("band") == 13
        history = harness.db.recent_telemetry()
        assert history and history[0]["rsrp"] == -94


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

    def test_reinserted_sim_is_reprobed_and_reconnects(self, harness):
        harness.run_until(State.CONNECTED)
        # Remove the SIM -> detected, back to MODEM_FOUND.
        harness.driver.sim_present = False
        harness.tick()
        assert harness.daemon.state is State.MODEM_FOUND
        # Insert a SIM the modem won't report until it's re-probed (no detect pin).
        harness.driver.sim_present = True
        harness.driver.needs_reprobe = True
        harness.tick(advance=61)  # past the reprobe interval -> nudge fires
        assert "REPROBE_SIM" in harness.driver.at_log
        assert harness.driver.needs_reprobe is False
        harness.run_until(State.CONNECTED)  # now sees the SIM and reconnects

    def test_no_sms_polling_without_sim(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.sim_present = False
        harness.tick()  # -> MODEM_FOUND, no SIM
        before = list(harness.driver.at_log)
        harness.driver.fail_all = False
        harness.tick(advance=120)  # SMS backstop interval would have elapsed
        # list_sms (AT+CMGF=0 / CMGL) must not be attempted without a SIM
        assert "AT+CMGF=0" not in harness.driver.at_log[len(before):]

    def test_imsi_only_change_detected(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.imsi = "310030000099999"  # same ICCID, new IMSI (profile swap)
        harness.tick()
        assert harness.daemon.state is State.SIM_READY
        harness.run_until(State.CONNECTED)


class TestOtaSwap:
    """The field bug: a Hologram OTA enables a new profile (new ICCID), the SIM
    refreshes, but v1 stayed CONNECTED on the stale ICCID and never reconnected."""

    def test_ota_swap_with_refresh_urc_reconnects(self, tmp_path):
        catchall = Profile.model_validate(
            {
                "name": "any",
                "match": {"iccid_patterns": ["*"], "priority": 1000},
                "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
            }
        )
        h = Harness(tmp_path, profiles=[catchall])
        h.run_until(State.CONNECTED)
        assert h.store.get().iccid == DEFAULT_ICCID

        h.driver.ota_swap("8946420000000000123")  # new ICCID + a +QSIMSTAT URC
        h.tick()  # URC polled -> sim_refresh_pending; CONNECTED handler re-evaluates
        assert h.daemon.state is State.SIM_READY
        h.run_until(State.CONNECTED)
        assert h.store.get().iccid == "8946420000000000123"
        assert "ota" in h.event_kinds()

    def test_ota_swap_detected_even_if_iccid_reads_stale(self, tmp_path):
        # Worst case: AT port still reports the OLD ICCID after refresh, but the
        # refresh URC alone must still force a re-attach.
        h = Harness(tmp_path)
        h.run_until(State.CONNECTED)
        before = h.store.get().iccid
        h.driver.push_urc("sim_status", {"enabled": 1, "inserted": 1}, raw="+QSIMSTAT: 1,1")
        h.tick()
        assert h.daemon.state is State.SIM_READY  # re-attach forced despite stale ICCID
        h.run_until(State.CONNECTED)
        assert h.store.get().iccid == before

    def test_urcs_are_logged(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.push_urc("new_sms", {"storage": "ME", "index": 1}, raw='+CMTI: "ME",1')
        harness.tick()
        urcs = harness.db.recent_urcs()
        assert any(u["kind"] == "new_sms" for u in urcs)  # raw URC recorded for forensics

    def test_registration_urc_updates_store(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.push_urc(
            "registration", {"stat": 5, "label": "registered-roaming"}, raw="+CEREG: 5"
        )
        harness.tick()
        assert harness.store.get().registration == "registered-roaming"

    def test_event_reporting_enabled_once(self, harness):
        harness.run_until(State.CONNECTED)
        assert harness.driver.event_reporting_enabled is True

    def test_identity_history_records_swap(self, tmp_path):
        h = Harness(tmp_path, profiles=[
            Profile.model_validate({
                "name": "any", "match": {"iccid_patterns": ["*"], "priority": 1000},
                "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
            })
        ])
        h.run_until(State.CONNECTED)
        h.driver.ota_swap("8946420000000000999")
        h.tick()
        h.run_until(State.CONNECTED)
        history = h.db.recent_identity()
        iccids = [row["iccid"] for row in history]
        assert "8946420000000000999" in iccids
        assert any(row["reason"] == "ota-swap" for row in history)


VARIANT_PROFILE = Profile.model_validate(
    {
        "name": "verizon-direct",
        "match": {"iccid_patterns": ["891480*"], "priority": 10},
        "pdp_contexts": [{"cid": 1, "apn": "vzwinternet", "bearer": True}],
        "pdp_variants": [
            {
                "name": "plan-b",
                "pdp_contexts": [{"cid": 1, "apn": "we01.vzwstatic", "bearer": True}],
            },
        ],
    }
)


class TestPdpVariants:
    def test_first_variant_used_when_it_connects(self, tmp_path):
        h = Harness(tmp_path, profiles=[VARIANT_PROFILE])
        h.driver.iccid = "8914800000000000123"
        h.run_until(State.CONNECTED)
        assert h.backend.configured_bearer.apn == "vzwinternet"

    def test_falls_through_to_second_variant(self, tmp_path):
        h = Harness(tmp_path, profiles=[VARIANT_PROFILE])
        h.driver.iccid = "8914800000000000123"

        # Fail connect while APN is the first variant; succeed on the second.
        def maybe_fail():
            h.backend.fail_connect = h.backend.configured_bearer.apn == "vzwinternet"

        original_connect = h.backend.connect
        def patched_connect():
            maybe_fail()
            return original_connect()
        h.backend.connect = patched_connect

        h.run_until(State.CONNECTED, max_ticks=30)
        assert h.backend.configured_bearer.apn == "we01.vzwstatic"
        # variant cycling should not have burned a supervisor failure
        assert h.daemon.supervisor.failures == 0


class TestLastDitchFallback:
    def _profile(self):
        return Profile.model_validate({
            "name": "strict", "match": {"iccid_patterns": ["8944500*"], "priority": 10},
            "pdp_contexts": [{"cid": 1, "apn": "broken.apn", "bearer": True}],
        })

    def _run_until_parked_and_degraded(self, h, max_ticks=120):
        for _ in range(max_ticks):
            h.tick(advance=400)
            if h.daemon.supervisor.parked and h.daemon.state is State.DEGRADED:
                return
        raise AssertionError("supervisor never parked in DEGRADED")

    def test_falls_back_to_builtin_default_after_ladder(self, tmp_path):
        h = Harness(tmp_path, profiles=[self._profile()])
        h.backend.fail_connect = True
        self._run_until_parked_and_degraded(h)
        h.backend.fail_connect = False
        h.tick()  # DEGRADED notices parked -> switch to builtin default
        assert h.daemon.active_profile.name == "builtin-default"
        h.run_until(State.CONNECTED, max_ticks=6)
        assert h.backend.configured_bearer.apn == "hologram"

    def test_no_fallback_when_disabled(self, tmp_path):
        h = Harness(tmp_path, profiles=[self._profile()])
        h.daemon.config.daemon.fallback_to_default_profile = False
        h.backend.fail_connect = True
        self._run_until_parked_and_degraded(h)
        h.tick()
        assert h.daemon.active_profile.name == "strict"  # never switched


class TestSmsFlow:
    def test_incoming_sms_syncs_inbox(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.receive_sms("+12025550123", "hello from the field")
        harness.tick()  # +CMTI -> sms_pending -> fetch
        rows = harness.db.recent_sms()
        assert any(r["body"] == "hello from the field" for r in rows)
        assert harness.store.get().sms_unread >= 1

    def test_unread_count_and_read_state_survives_resync(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.receive_sms("+12025550123", "hello")
        harness.tick()
        assert harness.store.get().sms_unread == 1
        # Mark read, then force a re-sync of the same message: it stays read.
        harness.queue.put(cmd.MarkSmsRead())
        harness.tick()
        assert harness.store.get().sms_unread == 0
        harness.queue.put(cmd.RefreshSms())
        harness.tick()
        assert harness.store.get().sms_unread == 0  # not re-marked unread
        rows = [r for r in harness.db.recent_sms() if r["direction"] == "in"]
        assert len(rows) == 1  # not duplicated on resync

    def test_new_sms_increments_unread_again(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.receive_sms("+1", "a")
        harness.tick()
        harness.queue.put(cmd.MarkSmsRead())
        harness.tick()
        harness.driver.receive_sms("+2", "b")  # a genuinely new one
        harness.tick()
        assert harness.store.get().sms_unread == 1

    def test_send_sms_command(self, harness):
        harness.run_until(State.CONNECTED)
        harness.queue.put(cmd.SendSms(number="+12025550123", text="ping"))
        harness.tick()
        assert harness.driver.sent_log == [("+12025550123", "ping")]
        rows = harness.db.recent_sms()
        assert any(r["direction"] == "out" and r["body"] == "ping" for r in rows)

    def test_delete_inbound_sms(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.receive_sms("+1", "x")
        harness.tick()
        row = next(r for r in harness.db.recent_sms() if r["direction"] == "in")
        harness.queue.put(cmd.DeleteSms(row_id=row["id"]))
        harness.tick()  # deletes from modem + marks resync
        harness.tick()  # resync runs
        assert harness.driver.list_sms() == []
        assert not [r for r in harness.db.recent_sms() if r["direction"] == "in"]

    def test_set_and_persist_sim_name(self, harness):
        harness.run_until(State.CONNECTED)
        harness.queue.put(cmd.SetSimName(name="Warehouse Pi"))
        harness.tick()
        assert harness.store.get().sim_name == "Warehouse Pi"
        assert harness.db.get_sim_name(DEFAULT_ICCID) == "Warehouse Pi"

    def test_sim_name_resolves_on_swap(self, tmp_path):
        h = Harness(tmp_path, profiles=[
            Profile.model_validate({
                "name": "any", "match": {"iccid_patterns": ["*"], "priority": 1000},
                "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
            })
        ])
        h.run_until(State.CONNECTED)
        # Name the second SIM ahead of time, then OTA-swap to it.
        h.db.set_sim_name("8946420000000000777", "Backup SIM")
        h.driver.ota_swap("8946420000000000777")
        h.tick()
        h.run_until(State.CONNECTED)
        assert h.store.get().sim_name == "Backup SIM"

    def test_clear_all_sms(self, harness):
        harness.run_until(State.CONNECTED)
        harness.driver.receive_sms("+1", "a")
        harness.driver.receive_sms("+2", "b")
        harness.tick()
        harness.queue.put(cmd.ClearSms())
        harness.tick()
        harness.tick()
        assert harness.driver.list_sms() == []


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
