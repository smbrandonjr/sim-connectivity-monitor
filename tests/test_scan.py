import time

import pytest

from sim_monitor.scan import net
from sim_monitor.scan.manager import ScanManager
from sim_monitor.system.host import collect_interface_ips

_IP_ADDR_JSON = """[
  {"ifname":"lo","addr_info":[{"family":"inet","local":"127.0.0.1"}]},
  {"ifname":"eth0","addr_info":[{"family":"inet","local":"192.168.1.50"}]},
  {"ifname":"wlan0","addr_info":[{"family":"inet6","local":"fe80::1"},
                                  {"family":"inet","local":"192.168.1.51"}]},
  {"ifname":"wwan0","addr_info":[{"family":"inet","local":"10.170.42.7"}]}
]"""


class TestInterfaceIps:
    def test_maps_present_interfaces(self):
        ips = collect_interface_ips(runner=lambda *a, **k: _IP_ADDR_JSON)
        assert ips == {"eth0_ip": "192.168.1.50", "wlan0_ip": "192.168.1.51",
                       "wwan0_ip": "10.170.42.7"}  # lo skipped; ipv4 only

    def test_empty_when_ip_unavailable(self):
        def boom(*a, **k):
            raise OSError("no ip command")
        assert collect_interface_ips(runner=boom) == {}


class TestParsePorts:
    def test_common_default(self):
        assert net.parse_ports("common") == net.COMMON_PORTS
        assert net.parse_ports("") == net.COMMON_PORTS

    def test_list(self):
        assert net.parse_ports("22,80,443") == [22, 80, 443]

    def test_range(self):
        assert net.parse_ports("80-82") == [80, 81, 82]

    def test_mixed_dedup_sorted(self):
        assert net.parse_ports("443,22,80,80-81") == [22, 80, 81, 443]

    def test_invalid(self):
        with pytest.raises(ValueError):
            net.parse_ports("70000")
        with pytest.raises(ValueError):
            net.parse_ports("abc")


class TestExpandHosts:
    def test_single_ip(self):
        assert net.expand_hosts("10.0.0.5") == ["10.0.0.5"]

    def test_slash30(self):
        hosts = net.expand_hosts("192.168.1.0/30")
        assert hosts == ["192.168.1.1", "192.168.1.2"]

    def test_too_large_rejected(self):
        with pytest.raises(ValueError, match="max"):
            net.expand_hosts("10.0.0.0/8")

    def test_invalid(self):
        with pytest.raises(ValueError):
            net.expand_hosts("not-a-cidr")


def _wait_done(mgr, timeout=3.0):
    start = time.time()
    while time.time() - start < timeout:
        if not mgr.status()["running"]:
            return mgr.status()
        time.sleep(0.02)
    raise AssertionError("scan did not finish")


class TestManagerSimulate:
    def test_discovery(self):
        mgr = ScanManager(simulate=True)
        mgr.start_discovery("192.168.1.0/24", net.parse_ports("common"))
        s = _wait_done(mgr)
        assert s["kind"] == "discovery"
        assert s["results"] and "ip" in s["results"][0]
        assert s["summary"]["scanned"] == 254

    def test_reachability(self):
        mgr = ScanManager(simulate=True)
        mgr.start_reachability("example.com", "wwan0")
        s = _wait_done(mgr)
        assert s["summary"]["http"]["status"] == 200
        assert s["summary"]["ping"]["loss_pct"] == 0.0

    def test_traceroute(self):
        mgr = ScanManager(simulate=True)
        mgr.start_traceroute("example.com", "wwan0")
        s = _wait_done(mgr)
        assert s["summary"]["reached"] is True
        assert [h["ttl"] for h in s["results"]] == [1, 2, 3, 4]

    def test_ports(self):
        mgr = ScanManager(simulate=True)
        mgr.start_ports("192.168.1.1", net.parse_ports("1-100"))
        s = _wait_done(mgr)
        assert {r["port"] for r in s["results"]} == {22, 80, 443}

    def test_rejects_concurrent_scan(self):
        mgr = ScanManager(simulate=True)
        mgr.start_traceroute("example.com", None)  # takes ~0.2s in sim
        with pytest.raises(RuntimeError, match="already running"):
            mgr.start_discovery("192.168.1.0/24", [80])
        _wait_done(mgr)

    def test_interfaces_simulated(self):
        mgr = ScanManager(simulate=True)
        names = {i["name"] for i in mgr.interfaces()}
        assert "wwan0" in names
