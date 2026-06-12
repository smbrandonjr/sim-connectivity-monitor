"""mmcli/nmcli/ip wrappers against captured fixture output, and RealBackend
against a recording fake runner."""

import pytest

from sim_monitor.config.schema import Profile
from sim_monitor.system.backend import BackendError
from sim_monitor.system.mmcli import Mmcli
from sim_monitor.system.nmcli import Nmcli, _parse_terse
from sim_monitor.system.real_backend import RealBackend, effective_metric
from sim_monitor.system.routing import (
    Routing,
    parse_default_routes,
    preferred_default,
)


class FakeRunner:
    """Returns canned stdout per command prefix; records every invocation."""

    def __init__(self, canned=None):
        self.canned = canned or {}  # "space-joined prefix" -> stdout or Exception
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        joined = " ".join(args)
        for prefix, output in self.canned.items():
            if joined.startswith(prefix):
                if isinstance(output, Exception):
                    raise output
                return output
        return ""

    def commands(self):
        return [" ".join(c) for c in self.calls]


MMCLI_LIST = '{"modem-list":["/org/freedesktop/ModemManager1/Modem/0"]}\n'
MMCLI_LIST_EMPTY = '{"modem-list":[]}\n'
MMCLI_MODEM = '{"modem":{"generic":{"state":"connected","model":"EC25"}}}\n'


class TestMmcli:
    def test_list_modems(self):
        mm = Mmcli(FakeRunner({"mmcli -L": MMCLI_LIST}))
        assert mm.list_modems() == [0]
        assert mm.first_modem() == 0

    def test_empty_list(self):
        mm = Mmcli(FakeRunner({"mmcli -L": MMCLI_LIST_EMPTY}))
        assert mm.first_modem() is None

    def test_modem_state(self):
        mm = Mmcli(FakeRunner({"mmcli -m 0": MMCLI_MODEM}))
        assert mm.modem_state(0) == "connected"

    def test_bad_json_raises(self):
        mm = Mmcli(FakeRunner({"mmcli -L": "garbage"}))
        with pytest.raises(BackendError, match="unparseable"):
            mm.list_modems()

    def test_enable_disable_commands(self):
        runner = FakeRunner()
        mm = Mmcli(runner)
        mm.disable(0)
        mm.enable(0)
        assert runner.commands() == ["mmcli -m 0 --disable", "mmcli -m 0 --enable"]


NMCLI_SHOW_ACTIVE = (
    "GENERAL.STATE:activated\n"
    "GENERAL.DEVICES:cdc-wdm0\n"
    "IP4.ADDRESS[1]:10.170.42.7/30\n"
)
NMCLI_SHOW_DOWN = "GENERAL.STATE:\nGENERAL.DEVICES:\n"
NMCLI_DEV_IFACE = "GENERAL.IP-IFACE:wwan0\n"


class TestNmcli:
    def test_parse_terse_escaped_colons(self):
        fields = _parse_terse("GENERAL.STATE:activated\nX:a\\:b\\:c\n")
        assert fields["GENERAL.STATE"] == "activated"
        assert fields["X"] == "a:b:c"

    def test_connection_state_active(self):
        runner = FakeRunner(
            {
                "nmcli -t -f GENERAL.STATE,GENERAL.DEVICES,IP4.ADDRESS": NMCLI_SHOW_ACTIVE,
                "nmcli -t -f GENERAL.IP-IFACE device show cdc-wdm0": NMCLI_DEV_IFACE,
            }
        )
        state = Nmcli(runner).connection_state()
        assert state.active
        assert state.interface == "wwan0"
        assert state.ip_address == "10.170.42.7"

    def test_connection_state_not_activated(self):
        runner = FakeRunner(
            {"nmcli -t -f GENERAL.STATE,GENERAL.DEVICES,IP4.ADDRESS": NMCLI_SHOW_DOWN}
        )
        assert Nmcli(runner).connection_state().active is False

    def test_connection_state_unknown_connection(self):
        runner = FakeRunner(
            {
                "nmcli -t -f GENERAL.STATE,GENERAL.DEVICES,IP4.ADDRESS": BackendError(
                    "unknown connection"
                )
            }
        )
        assert Nmcli(runner).connection_state().active is False

    def test_ensure_creates_then_modifies_when_missing(self):
        runner = FakeRunner({"nmcli -t -f NAME connection show": "Wired connection 1\n"})
        Nmcli(runner).ensure_gsm_connection(apn="hologram", metric=50)
        commands = runner.commands()
        assert any(c.startswith("nmcli connection add type gsm") for c in commands)
        assert any("ipv4.route-metric 50" in c for c in commands)

    def test_ensure_only_modifies_when_present(self):
        runner = FakeRunner(
            {"nmcli -t -f NAME connection show": "sim-monitor-cellular\n"}
        )
        Nmcli(runner).ensure_gsm_connection(apn="hologram", metric=50)
        assert not any("connection add" in c for c in runner.commands())

    def test_down_swallows_not_active(self):
        runner = FakeRunner(
            {"nmcli connection down": BackendError("'x' is not an active connection")}
        )
        Nmcli(runner).down()  # must not raise

    def test_down_propagates_other_errors(self):
        runner = FakeRunner({"nmcli connection down": BackendError("dbus timeout")})
        with pytest.raises(BackendError, match="dbus"):
            Nmcli(runner).down()


IP_ROUTE_BOTH = (
    '[{"dst":"default","gateway":"10.170.42.8","dev":"wwan0","metric":50},'
    '{"dst":"default","gateway":"192.168.1.1","dev":"eth0","metric":100}]'
)


class TestRouting:
    def test_parse_and_preference(self):
        routes = parse_default_routes(IP_ROUTE_BOTH)
        assert len(routes) == 2
        assert preferred_default(routes).interface == "wwan0"

    def test_empty_output(self):
        assert parse_default_routes("") == []
        assert preferred_default([]) is None

    def test_interface_is_default(self):
        runner = FakeRunner({"ip -j route show default": IP_ROUTE_BOTH})
        routing = Routing(runner)
        assert routing.interface_is_default("wwan0") is True
        assert routing.interface_is_default("eth0") is False


PROFILE = Profile.model_validate(
    {
        "name": "x",
        "pdp_contexts": [
            {"cid": 1, "apn": "hologram", "auth": "pap",
             "username": "u", "password": "p", "bearer": True},
        ],
    }
)


def make_backend(canned=None):
    runner = FakeRunner(canned or {})
    backend = RealBackend(
        Mmcli(runner), Nmcli(runner), Routing(runner), at_port_provider=lambda: None
    )
    return backend, runner


class TestRealBackend:
    def test_effective_metric(self):
        assert effective_metric(PROFILE) == 50
        off = PROFILE.model_copy(deep=True)
        off.routing.make_default = False
        assert effective_metric(off) == 700

    def test_configure_passes_bearer_credentials(self):
        backend, runner = make_backend(
            {"nmcli -t -f NAME connection show": "sim-monitor-cellular\n"}
        )
        backend.configure_connection(PROFILE)
        modify = next(c for c in runner.commands() if "connection modify" in c)
        assert "gsm.apn hologram" in modify
        assert "gsm.username u" in modify
        assert "gsm.password p" in modify

    def test_modem_disable_enable_order(self):
        backend, runner = make_backend({"mmcli -L": MMCLI_LIST})
        backend.modem_disable_enable()
        commands = [c for c in runner.commands() if "--disable" in c or "--enable" in c]
        assert commands == ["mmcli -m 0 --disable", "mmcli -m 0 --enable"]

    def test_modem_disable_enable_without_modem_raises(self):
        backend, _ = make_backend({"mmcli -L": MMCLI_LIST_EMPTY})
        with pytest.raises(BackendError, match="no modem"):
            backend.modem_disable_enable()

    def test_usb_cycle_without_port_raises(self):
        backend, _ = make_backend()
        with pytest.raises(BackendError, match="no AT port"):
            backend.usb_power_cycle()

    def test_verify_routing_inverted_when_not_default(self):
        canned = {
            "nmcli -t -f GENERAL.STATE,GENERAL.DEVICES,IP4.ADDRESS": NMCLI_SHOW_ACTIVE,
            "nmcli -t -f GENERAL.IP-IFACE device show cdc-wdm0": NMCLI_DEV_IFACE,
            "ip -j route show default": IP_ROUTE_BOTH,  # wwan0 is preferred default
        }
        backend, _ = make_backend(canned)
        assert backend.verify_routing(PROFILE) is True  # make_default: wwan0 must win
        off = PROFILE.model_copy(deep=True)
        off.routing.make_default = False
        assert backend.verify_routing(off) is False  # must NOT be default, but is
