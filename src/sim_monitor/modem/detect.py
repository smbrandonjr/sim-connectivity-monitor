"""Find the modem's dedicated AT port and pick the right vendor driver.

AT port resolution order:
1. Explicit `modem.at_port` from config.yaml.
2. /dev/sim-monitor-at — the udev symlink installed by deploy/udev rules
   (preferred: survives ttyUSB renumbering and is already MM-ignored).
3. Known USB VID/PID + interface-number hints over enumerated serial ports.

Driver selection: USB VID first, then AT+CGMI sniff, else generic 3GPP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sim_monitor.modem import at_parser
from sim_monitor.modem.at_channel import ATChannel
from sim_monitor.modem.at_driver import ATModemDriver
from sim_monitor.modem.driver_base import ModemDetector, ModemDriver, ModemError
from sim_monitor.modem.drivers import ALL_DRIVERS
from sim_monitor.system.mmcli import Mmcli

log = logging.getLogger(__name__)

UDEV_SYMLINK = "/dev/sim-monitor-at"

# (vid, pid) -> USB interface number of a spare AT-capable port that
# ModemManager does not need. Mirrors deploy/udev/77-sim-monitor.rules.
AT_PORT_HINTS: dict[tuple[int, int], int] = {
    (0x2C7C, 0x0125): 3,  # Quectel EC25 / EG25-G
    (0x2C7C, 0x0121): 3,  # Quectel EC21
    (0x2C7C, 0x0296): 2,  # Quectel BG96
    (0x1E0E, 0x9001): 3,  # SIMCOM SIM7600
    (0x1E0E, 0x9011): 3,  # SIMCOM SIM7600 (RNDIS composition)
    (0x1E0E, 0x9206): 5,  # SIMCOM SIM7070/7080/7090 (if05 = MM's secondary AT port)
    (0x1BC7, 0x1201): 4,  # Telit LE910C1
    (0x1BC7, 0x1031): 2,  # Telit LE910C1-WWX (if00 DIAG, if01/if02 AT, cdc-wdm QMI)
    (0x1BC7, 0x0036): 4,  # Telit LE910C4
}

_CGMI_HINTS = {"QUECTEL": "quectel", "SIMCOM": "simcom", "TELIT": "telit"}


@dataclass(frozen=True)
class SerialPortInfo:
    """The subset of pyserial's ListPortInfo we use (testable without hardware)."""

    device: str
    vid: int | None
    pid: int | None
    location: str | None  # e.g. "1-1.2:1.3" -> interface 3


@dataclass(frozen=True)
class ScannedPort:
    """One serial port enriched for the UI's modem-setup view."""

    device: str
    vid: int | None
    pid: int | None
    interface: int | None
    mm_claimed: bool   # ModemManager listed this port (avoid taking it)
    is_current: bool   # the port sim-monitor is using right now


def interface_number(location: str | None) -> int | None:
    if not location or "." not in location:
        return None
    try:
        return int(location.rsplit(".", 1)[-1])
    except ValueError:
        return None


def pick_at_port(ports: list[SerialPortInfo]) -> SerialPortInfo | None:
    """Choose the hinted AT interface for a known modem among serial ports."""
    for port in ports:
        if port.vid is None or port.pid is None:
            continue
        hint = AT_PORT_HINTS.get((port.vid, port.pid))
        if hint is not None and interface_number(port.location) == hint:
            return port
    return None


def driver_class_for(vid: int | None, cgmi: str | None) -> type[ATModemDriver]:
    if vid is not None:
        for cls in ALL_DRIVERS:
            if vid in cls.VENDOR_IDS:
                return cls
    if cgmi:
        for marker, name in _CGMI_HINTS.items():
            if marker in cgmi.upper():
                return next(c for c in ALL_DRIVERS if c.name == name)
    return ATModemDriver


def _list_serial_ports() -> list[SerialPortInfo]:
    from serial.tools import list_ports

    return [
        SerialPortInfo(
            device=p.device, vid=p.vid, pid=p.pid, location=getattr(p, "location", None)
        )
        for p in list_ports.comports()
    ]


class RealDetector(ModemDetector):
    def __init__(
        self,
        mmcli: Mmcli,
        at_port: str = "auto",
        baud: int = 115200,
        channel_factory=ATChannel,
        port_lister=_list_serial_ports,
    ) -> None:
        self.mmcli = mmcli
        self.at_port = at_port
        self.baud = baud
        self.channel_factory = channel_factory
        self.port_lister = port_lister
        self.last_at_port: str | None = None  # consumed by USB power-cycle

    def detect(self) -> ModemDriver | None:
        if self.mmcli.first_modem() is None:
            return None  # nothing enumerated yet; keep polling

        port_device, vid = self._resolve_at_port()
        channel = self.channel_factory(port_device, self.baud)
        channel.open()
        channel.execute("ATE0")  # disable echo for clean parsing

        cgmi = None
        if vid is None:
            try:
                cgmi = at_parser.parse_cgmi(channel.execute("AT+CGMI"))
            except (ModemError, at_parser.ATParseError):
                pass
        driver_cls = driver_class_for(vid, cgmi)
        self.last_at_port = port_device
        log.info("using AT port %s with %s driver", port_device, driver_cls.name)
        return driver_cls(channel)

    def scan_ports(self, current_at_port: str | None) -> tuple[bool, list[ScannedPort]]:
        """Enumerate serial ports for the UI's modem-setup view, flagging which
        ones ModemManager claimed (don't take those) and which one we use now.
        Read-only; safe to call from the daemon thread anytime."""
        try:
            modem_index = self.mmcli.first_modem()
        except Exception:  # noqa: BLE001 - mmcli hiccup must not break the scan
            modem_index = None
        claimed: set[str] = set()
        if modem_index is not None:
            try:
                claimed = {Path(p).name for p in self.mmcli.modem_ports(modem_index)}
            except Exception:  # noqa: BLE001
                claimed = set()
        current_real = str(Path(current_at_port).resolve()) if current_at_port else None
        scanned = []
        for p in self.port_lister():
            is_current = current_at_port is not None and (
                p.device == current_at_port or str(Path(p.device).resolve()) == current_real
            )
            scanned.append(
                ScannedPort(
                    device=p.device, vid=p.vid, pid=p.pid,
                    interface=interface_number(p.location),
                    mm_claimed=Path(p.device).name in claimed,
                    is_current=is_current,
                )
            )
        scanned.sort(key=lambda s: (s.interface if s.interface is not None else 99, s.device))
        return modem_index is not None, scanned

    def probe(self, device: str) -> tuple[bool, str | None, str | None]:
        """Open `device` and ask the modem who it is. Returns
        (responded, identity, detail). Never raises — a dead/busy port just
        reports responded=False with a reason."""
        try:
            channel = self.channel_factory(device, self.baud)
        except Exception as e:  # noqa: BLE001
            return False, None, str(e)
        try:
            channel.open()
            channel.execute("ATE0")
            parts = []
            for cmd in ("AT+CGMI", "AT+CGMM"):
                try:
                    lines = [ln for ln in channel.execute(cmd) if ln.strip()]
                    if lines:
                        parts.append(lines[0].strip())
                except (ModemError, at_parser.ATParseError):
                    pass
            identity = " ".join(dict.fromkeys(parts)) or "responded (unknown model)"
            return True, identity, None
        except ModemError as e:
            return False, None, str(e)
        finally:
            try:
                channel.close()
            except Exception:  # noqa: BLE001
                pass

    def _resolve_at_port(self) -> tuple[str, int | None]:
        ports = self.port_lister()

        def vid_of(device: str) -> int | None:
            real = str(Path(device).resolve())
            for p in ports:
                if p.device == device or str(Path(p.device).resolve()) == real:
                    return p.vid
            return None

        if self.at_port != "auto":
            if not Path(self.at_port).exists():
                raise ModemError(f"configured modem.at_port {self.at_port} does not exist")
            return self.at_port, vid_of(self.at_port)

        if Path(UDEV_SYMLINK).exists():
            return UDEV_SYMLINK, vid_of(UDEV_SYMLINK)

        hinted = pick_at_port(ports)
        if hinted is not None:
            return hinted.device, hinted.vid

        raise ModemError(
            "modem is visible to ModemManager but no AT port was found: install the "
            "udev rules (deploy/udev) or set modem.at_port in config.yaml"
        )
