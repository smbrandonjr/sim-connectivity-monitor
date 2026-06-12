from sim_monitor.core.state_store import Snapshot, StateStore, derive_status
from sim_monitor.core.states import State


def snap(**kwargs):
    store = StateStore()
    if "state" in kwargs:
        store.set_state(kwargs.pop("state"))
    store.update(**kwargs)
    return store.get()


class TestDeriveStatus:
    def test_connected_with_operator(self):
        status, message = derive_status(snap(state=State.CONNECTED, operator="Hologram"))
        assert status == "connected"
        assert message == "cellular connected via Hologram"

    def test_connected_without_operator(self):
        status, message = derive_status(snap(state=State.CONNECTED))
        assert status == "connected"
        assert message == "cellular connected"

    def test_fallback_test_is_not_degraded(self):
        status, message = derive_status(snap(state=State.FALLBACK_TEST))
        assert status == "fallback_test"
        assert "radio off" in message

    def test_degraded_with_reason(self):
        status, message = derive_status(
            snap(state=State.DEGRADED, last_error="connect failed: nmcli up timed out")
        )
        assert status == "degraded"
        assert message == "recovery in progress: connect failed: nmcli up timed out"

    def test_no_modem_default_message(self):
        status, message = derive_status(snap(state=State.NO_MODEM))
        assert status == "degraded"
        assert message == "no modem detected"

    def test_modem_found_with_sim_detail(self):
        status, message = derive_status(
            snap(state=State.MODEM_FOUND, last_error="no SIM inserted")
        )
        assert (status, message) == ("degraded", "modem found, waiting for SIM: no SIM inserted")

    def test_long_errors_are_truncated_to_first_line(self):
        ugly = "nmcli up failed (rc=10): " + "x" * 500 + "\nstack trace line\nmore"
        status, message = derive_status(snap(state=State.DEGRADED, last_error=ugly))
        assert status == "degraded"
        assert len(message) < 160
        assert "stack trace" not in message
        assert message.endswith("...")


class TestPlaceholderContext:
    def test_context_includes_status_fields(self):
        context = snap(state=State.DEGRADED, last_error="connection lost").placeholder_context()
        assert context["status"] == "degraded"
        assert context["status_message"] == "recovery in progress: connection lost"

    def test_default_snapshot_is_degraded_no_modem(self):
        context = Snapshot().placeholder_context()
        assert context["status"] == "degraded"
        assert context["status_message"] == "no modem detected"
