"""Serial-port scan + AT-probe helpers behind the web UI's modem-setup flow."""

import json

from sim_monitor.modem.detect import RealDetector, SerialPortInfo
from sim_monitor.modem.driver_base import ModemError
from sim_monitor.system.mmcli import Mmcli


class StubMmcli:
    def __init__(self, modem=0, ports=None):
        self._modem = modem
        self._ports = ports or []

    def first_modem(self):
        return self._modem

    def modem_ports(self, index):
        return self._ports


class FakeChannel:
    """Canned AT responses; records opens/closes so we can assert cleanup."""

    def __init__(self, port, baud, responses=None, open_error=None):
        self.port = port
        self.responses = responses or {}
        self.open_error = open_error
        self.closed = False

    def open(self):
        if self.open_error:
            raise self.open_error

    def execute(self, command, timeout=None):
        return self.responses.get(command, [])

    def close(self):
        self.closed = True


def _detector(ports, mmcli, channel_factory=None):
    return RealDetector(
        mmcli=mmcli,
        channel_factory=channel_factory or (lambda *a, **k: FakeChannel(*a)),
        port_lister=lambda: ports,
    )


PORTS = [
    SerialPortInfo("/dev/ttyUSB0", 0x1E0E, 0x9206, "1-1.2:1.0"),
    SerialPortInfo("/dev/ttyUSB2", 0x1E0E, 0x9206, "1-1.2:1.2"),
    SerialPortInfo("/dev/ttyUSB3", 0x1E0E, 0x9206, "1-1.2:1.3"),
]


class TestScanPorts:
    def test_marks_mm_claimed_and_current_and_interface(self):
        det = _detector(PORTS, StubMmcli(modem=0, ports=["ttyUSB0", "cdc-wdm0"]))
        present, scanned = det.scan_ports(current_at_port="/dev/ttyUSB3")
        assert present is True
        by_dev = {p.device: p for p in scanned}
        assert by_dev["/dev/ttyUSB0"].mm_claimed is True
        assert by_dev["/dev/ttyUSB2"].mm_claimed is False
        assert by_dev["/dev/ttyUSB3"].is_current is True
        assert by_dev["/dev/ttyUSB2"].interface == 2  # parsed from location

    def test_no_modem_present(self):
        det = _detector(PORTS, StubMmcli(modem=None))
        present, scanned = det.scan_ports(current_at_port=None)
        assert present is False
        assert len(scanned) == 3
        assert all(not p.mm_claimed for p in scanned)


class TestProbe:
    def test_identifies_responding_modem(self):
        responses = {"AT+CGMI": ["SIMCOM"], "AT+CGMM": ["SIM7080"]}
        det = _detector(
            PORTS, StubMmcli(),
            channel_factory=lambda *a, **k: FakeChannel(*a, responses=responses),
        )
        responded, identity, detail = det.probe("/dev/ttyUSB2")
        assert responded is True
        assert "SIMCOM" in identity and "SIM7080" in identity
        assert detail is None

    def test_dead_port_reports_no_response(self):
        det = _detector(
            PORTS, StubMmcli(),
            channel_factory=lambda *a, **k: FakeChannel(
                *a, open_error=ModemError("cannot open AT port")
            ),
        )
        responded, identity, detail = det.probe("/dev/ttyUSB9")
        assert responded is False
        assert identity is None
        assert "cannot open" in detail


def test_mmcli_modem_ports_parses_names():
    payload = {"modem": {"generic": {"ports": [
        "ttyUSB2 (at)", "ttyUSB3 (ppp)", "cdc-wdm0 (qmi)", "wwan0 (net)",
    ]}}}
    mm = Mmcli(runner=lambda *a, **k: json.dumps(payload))
    assert mm.modem_ports(0) == ["ttyUSB2", "ttyUSB3", "cdc-wdm0", "wwan0"]
