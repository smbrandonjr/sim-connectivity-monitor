import pytest

from sim_monitor.modem.at_driver import ATModemDriver
from sim_monitor.modem.detect import (
    RealDetector,
    SerialPortInfo,
    driver_class_for,
    interface_number,
    pick_at_port,
)
from sim_monitor.modem.driver_base import ModemError
from sim_monitor.modem.drivers import QuectelDriver, SimcomDriver, TelitDriver


class TestHelpers:
    def test_interface_number(self):
        assert interface_number("1-1.2:1.3") == 3
        assert interface_number("3-1:1.0") == 0
        assert interface_number(None) is None
        assert interface_number("weird") is None

    def test_pick_at_port_quectel_ec25(self):
        ports = [
            SerialPortInfo("/dev/ttyUSB0", 0x2C7C, 0x0125, "1-1.2:1.0"),
            SerialPortInfo("/dev/ttyUSB1", 0x2C7C, 0x0125, "1-1.2:1.1"),
            SerialPortInfo("/dev/ttyUSB2", 0x2C7C, 0x0125, "1-1.2:1.2"),
            SerialPortInfo("/dev/ttyUSB3", 0x2C7C, 0x0125, "1-1.2:1.3"),
        ]
        assert pick_at_port(ports).device == "/dev/ttyUSB3"

    def test_pick_at_port_unknown_modem(self):
        assert pick_at_port([SerialPortInfo("/dev/ttyUSB0", 0x1234, 0x5678, "1-1:1.0")]) is None

    def test_pick_at_port_ignores_non_usb(self):
        assert pick_at_port([SerialPortInfo("/dev/ttyAMA0", None, None, None)]) is None

    @pytest.mark.parametrize(
        "vid,cgmi,expected",
        [
            (0x2C7C, None, QuectelDriver),
            (0x1E0E, None, SimcomDriver),
            (0x1BC7, None, TelitDriver),
            (None, "Quectel", QuectelDriver),
            (None, "SIMCOM INCORPORATED", SimcomDriver),
            (None, "Telit Communications", TelitDriver),
            (None, "Acme Modems", ATModemDriver),
            (0x9999, None, ATModemDriver),
        ],
    )
    def test_driver_class_for(self, vid, cgmi, expected):
        assert driver_class_for(vid, cgmi) is expected


class FakeMmcli:
    def __init__(self, modem=0):
        self.modem = modem

    def first_modem(self):
        return self.modem


class FakeChannel:
    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self.executed = []

    def set_urc_handler(self, handler):
        pass

    def open(self):
        pass

    def execute(self, command, timeout=None):
        self.executed.append(command)
        if command == "AT+CGMI":
            return ["SIMCOM INCORPORATED"]
        return []


EC25_PORTS = [
    SerialPortInfo("/dev/ttyUSB2", 0x2C7C, 0x0125, "1-1.2:1.2"),
    SerialPortInfo("/dev/ttyUSB3", 0x2C7C, 0x0125, "1-1.2:1.3"),
]


class TestRealDetector:
    def test_no_modem_returns_none(self):
        detector = RealDetector(FakeMmcli(modem=None), port_lister=lambda: [])
        assert detector.detect() is None

    def test_detects_quectel_via_hints(self):
        detector = RealDetector(
            FakeMmcli(), channel_factory=FakeChannel, port_lister=lambda: EC25_PORTS
        )
        driver = detector.detect()
        assert isinstance(driver, QuectelDriver)
        assert driver.at.port == "/dev/ttyUSB3"
        assert detector.last_at_port == "/dev/ttyUSB3"
        assert "ATE0" in driver.at.executed

    def test_explicit_port_with_cgmi_sniff(self, tmp_path):
        port = tmp_path / "ttyFAKE"
        port.touch()
        detector = RealDetector(
            FakeMmcli(),
            at_port=str(port),
            channel_factory=FakeChannel,
            port_lister=lambda: [],  # port not in enumeration -> VID unknown -> sniff
        )
        driver = detector.detect()
        assert isinstance(driver, SimcomDriver)

    def test_explicit_port_missing_raises(self):
        detector = RealDetector(
            FakeMmcli(), at_port="/dev/does-not-exist", port_lister=lambda: []
        )
        with pytest.raises(ModemError, match="does not exist"):
            detector.detect()

    def test_no_port_found_raises_with_guidance(self):
        detector = RealDetector(FakeMmcli(), port_lister=lambda: [])
        with pytest.raises(ModemError, match="udev rules"):
            detector.detect()
