"""Scriptable in-memory modem for tests and --simulate mode.

Tests and the simulation harness mutate the public attributes directly to
script scenarios: SIM removal/swap, stray firmware PDP contexts, airplane
mode side effects (the Hologram fallback applet switching profiles), and
command failures.
"""

from __future__ import annotations

from sim_monitor.config.schema import PdpContext
from sim_monitor.modem import pdu
from sim_monitor.modem.at_parser import ActualPdpContext, SignalQuality
from sim_monitor.modem.driver_base import (
    ModemDetector,
    ModemDriver,
    ModemError,
    ModemIdentity,
    RawSms,
    SimStatus,
    UrcEvent,
)

DEFAULT_ICCID = "8944500612345678901"
FALLBACK_ICCID = "8944999900000000001"


class FakeModemDriver(ModemDriver):
    name = "fake"

    def __init__(self, iccid: str = DEFAULT_ICCID) -> None:
        self.identity = ModemIdentity(vendor="FakeCorp", model="FM100", imei="490154203237518")
        self.sim_present = True
        self.iccid = iccid
        self.imsi = "234500000000001"
        self.operator: str | None = "Hologram"
        self.signal = SignalQuality(rssi_dbm=-77, percent=58)
        self.airplane = False
        # Simulates firmware that boots with auto-created contexts.
        self.contexts: dict[int, ActualPdpContext] = {
            1: ActualPdpContext(cid=1, pdp_type="IPv4", apn="internet"),
            8: ActualPdpContext(cid=8, pdp_type="IPv4v6", apn="ims"),
        }
        # Scripting hooks
        self.fail_all = False  # every call raises ModemError (port wedged)
        self.fallback_iccid: str | None = None  # applied when airplane mode ends
        # set_airplane(False) raises this many times before succeeding (models a
        # dropped reply on radio re-enable); get_sim_status reports absent this
        # many times before the SIM session re-initializes after CFUN=1.
        self.airplane_off_failures = 0
        self.sim_ready_after = 0
        self.at_log: list[str] = []  # records init commands and resets
        self.event_reporting_enabled = False
        self._pending_urcs: list[UrcEvent] = []
        # When set, the modem won't report an inserted SIM until reprobe_sim()
        # (models a board without a SIM-detect pin — hot-swap needs a nudge).
        self.needs_reprobe = False
        # In-memory SMS store: index -> (status, pdu_hex). sent_log records outbound.
        self._sms: dict[int, tuple[int, str]] = {}
        self._next_sms_index = 1
        self.sent_log: list[tuple[str, str]] = []  # (number, text)

    def _check(self) -> None:
        if self.fail_all:
            raise ModemError("simulated modem failure")

    def get_identity(self) -> ModemIdentity:
        self._check()
        return self.identity

    def get_firmware(self) -> str:
        self._check()
        return "FM100R-FAKE-01.001.01"

    def get_sim_status(self) -> SimStatus:
        self._check()
        if self.sim_ready_after > 0:
            self.sim_ready_after -= 1
            return SimStatus(present=False, detail="SIM busy")
        if not self.sim_present or self.needs_reprobe:
            return SimStatus(present=False, detail="no SIM inserted")
        return SimStatus(present=True, iccid=self.iccid, imsi=self.imsi)

    def get_operator(self) -> str | None:
        self._check()
        return None if self.airplane else self.operator

    def get_signal(self) -> SignalQuality | None:
        self._check()
        return None if self.airplane else self.signal

    def get_telemetry(self) -> dict:
        self._check()
        if self.airplane:
            return {}
        return {
            "rat": "LTE", "rssi": -67, "rsrp": -94, "rsrq": -9, "sinr": 14,
            "band": 13, "cell_id": "0144C3D5", "pci": 176, "earfcn": 5110,
            "tac": "1A2B", "operator_numeric": "310260", "channel": 5110,
        }

    def get_pdp_contexts(self) -> list[ActualPdpContext]:
        self._check()
        return [self.contexts[cid] for cid in sorted(self.contexts)]

    def define_pdp_context(self, context: PdpContext) -> None:
        self._check()
        self.contexts[context.cid] = ActualPdpContext(
            cid=context.cid, pdp_type=context.pdp_type, apn=context.apn
        )

    def delete_pdp_context(self, cid: int) -> None:
        self._check()
        self.contexts.pop(cid, None)

    def supported_rats(self) -> list[str]:
        # The simulated modem pretends to be a do-everything module.
        return ["auto", "5g_sa", "5g_nsa", "lte", "lte_m", "nb_iot", "3g", "2g"]

    def set_rat(self, rat: str) -> None:
        self._check()
        if rat not in self.supported_rats():
            raise ModemError(f"unsupported RAT {rat!r}")
        self.at_log.append(f"SET_RAT:{rat}")

    def set_airplane(self, on: bool) -> None:
        self._check()
        if not on and self.airplane_off_failures > 0:
            self.airplane_off_failures -= 1
            raise ModemError("simulated radio re-enable failure")
        leaving = self.airplane and not on
        self.airplane = on
        if leaving and self.fallback_iccid:
            # The SIM applet switched profiles while radio was off.
            self.iccid = self.fallback_iccid
            self.imsi = "234509999999999"
            self.fallback_iccid = None

    def full_reset(self) -> None:
        self._check()
        self.at_log.append("RESET")
        self.airplane = False

    def reprobe_sim(self) -> None:
        self._check()
        self.at_log.append("REPROBE_SIM")
        self.needs_reprobe = False  # the nudge makes the inserted SIM visible

    def clear_forbidden_plmn(self) -> None:
        self._check()
        self.at_log.append("CLEAR_FPLMN")

    def run_init_commands(self, commands: list[str]) -> None:
        self._check()
        self.at_log.extend(commands)

    def execute_raw(self, command: str, timeout: float | None = None) -> list[str]:
        self._check()
        self.at_log.append(command)
        canned = {
            "AT+CSQ": ["+CSQ: 18,99"],
            "AT+CREG?": ["+CREG: 0,5"],
            "AT+CEREG?": ["+CEREG: 0,5"],
            "AT+COPS?": ['+COPS: 0,0,"Hologram",7'],
            "AT+CPIN?": ["+CPIN: READY"],
            "AT+CGDCONT?": [
                f'+CGDCONT: {c.cid},"{c.pdp_type}","{c.apn}"'
                for c in self.get_pdp_contexts()
            ],
        }
        return canned.get(command, [f"{command}: simulated OK"])

    def enable_event_reporting(self) -> None:
        self._check()
        self.event_reporting_enabled = True

    def poll_events(self) -> list[UrcEvent]:
        self._check()
        events, self._pending_urcs = self._pending_urcs, []
        return events

    def list_sms(self) -> list[RawSms]:
        self._check()
        return [RawSms(idx, st, p) for idx, (st, p) in sorted(self._sms.items())]

    def send_sms(self, number: str, text: str) -> int:
        self._check()
        self.sent_log.append((number, text))
        return len(pdu.encode_submit(number, text))

    def delete_sms(self, index: int) -> None:
        self._check()
        self._sms.pop(index, None)

    def delete_all_sms(self) -> None:
        self._check()
        self._sms.clear()

    # ── test/sim scripting helpers ──────────────────────────────────────────
    def receive_sms(self, sender: str, text: str) -> int:
        """Simulate an incoming SMS: store its PDU and queue a +CMTI URC."""
        # Build a DELIVER PDU around the SUBMIT body (good enough for decoding).
        submit_hex, _ = pdu.encode_submit(sender, text)[0]
        b = bytes.fromhex(submit_hex)
        oa_octets = 1 + (b[3] + 1) // 2
        idx = 4 + oa_octets + 2  # past first/MR/DA/PID/DCS
        dcs = b[4 + oa_octets + 1]
        udl = b[idx]
        ud = b[idx + 1:]
        oa = b[3:4 + oa_octets].hex().upper()
        deliver = ("0004" + oa + "00" + f"{dcs:02X}" + "00000000000000"
                   + f"{udl:02X}" + ud.hex().upper())
        index = self._next_sms_index
        self._next_sms_index += 1
        self._sms[index] = (0, deliver)  # 0 = unread
        self.push_urc("new_sms", {"storage": "ME", "index": index}, raw=f'+CMTI: "ME",{index}')
        return index

    # Fake SCP80 secured command packet behind a UDH 0x70 information element,
    # as a carrier OTA platform would send (contents are arbitrary bytes).
    _OTA_UD_HEX = "027000" + "00281506192525B00010A1B2C3D4"

    def receive_ota_sms(self) -> int:
        """Simulate a carrier SIM/eUICC OTA message: SMS-DELIVER with
        PID 0x7F ((U)SIM data download), DCS 0xF6 (8-bit, class 2), and a
        23.048 command-packet UDH — the shape of real SM-SR/RAM traffic."""
        ud = bytes.fromhex(self._OTA_UD_HEX)
        deliver = (
            "00"    # no SMSC
            "44"    # SMS-DELIVER, UDHI set
            "03" "81" "77F0"  # OA: short code 770
            "7F"    # TP-PID: (U)SIM data download
            "F6"    # TP-DCS: 8-bit data, message class 2
            "00000000000000"  # zeroed SCTS (decoder falls back to now)
            + f"{len(ud):02X}" + ud.hex().upper()
        )
        index = self._next_sms_index
        self._next_sms_index += 1
        self._sms[index] = (0, deliver)  # 0 = unread
        self.push_urc("new_sms", {"storage": "ME", "index": index}, raw=f'+CMTI: "ME",{index}')
        return index

    # ── test/sim scripting helpers ──────────────────────────────────────────
    def push_urc(self, kind: str, fields: dict | None = None, raw: str = "") -> None:
        """Queue a URC the daemon will see on its next poll_events()."""
        self._pending_urcs.append(UrcEvent(raw=raw or kind, kind=kind, fields=fields or {}))

    def ota_swap(self, new_iccid: str, new_imsi: str = "310030000000001") -> None:
        """Simulate a Hologram OTA: profile swapped to a new ICCID/IMSI and the
        modem emits a SIM-refresh URC — exactly the field scenario where the
        old code stayed CONNECTED on a stale ICCID."""
        self.iccid = new_iccid
        self.imsi = new_imsi
        self.push_urc("sim_status", {"enabled": 1, "inserted": 1}, raw="+QSIMSTAT: 1,1")


class FakeDetector(ModemDetector):
    """Returns the fake driver, optionally only after N detect() calls."""

    def __init__(self, driver: FakeModemDriver | None, appear_after: int = 0) -> None:
        self.driver = driver
        self.appear_after = appear_after
        self.calls = 0
        self.at_port = "auto"
        self.last_at_port = "/dev/ttyUSB-fake"

    def detect(self) -> ModemDriver | None:
        self.calls += 1
        if self.driver is None or self.calls <= self.appear_after:
            return None
        return self.driver

    def scan_ports(self, current_at_port):
        """Simulated SIM7080-style 6-port layout for --simulate and tests."""
        from sim_monitor.modem.detect import ScannedPort

        present = self.driver is not None
        ports = [
            ScannedPort("/dev/ttyUSB0", 0x1E0E, 0x9206, 0, mm_claimed=True, is_current=False),
            ScannedPort("/dev/ttyUSB1", 0x1E0E, 0x9206, 1, mm_claimed=False, is_current=False),
            ScannedPort("/dev/ttyUSB2", 0x1E0E, 0x9206, 2, mm_claimed=False,
                        is_current=current_at_port == "/dev/ttyUSB2"),
            ScannedPort("/dev/ttyUSB3", 0x1E0E, 0x9206, 3, mm_claimed=False,
                        is_current=current_at_port == "/dev/ttyUSB3"),
        ]
        return present, ports

    def probe(self, device: str):
        if device in ("/dev/ttyUSB2", "/dev/ttyUSB3"):
            return True, "SIMCOM SIM7080", None
        return False, None, "no response from port"
