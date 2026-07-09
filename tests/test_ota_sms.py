"""SIM/eUICC OTA SMS detection: DCS class decoding, classification, PDU field
retention, inbox flagging, and the URC-side PDU classifier."""

from sim_monitor.modem import pdu
from sim_monitor.modem.at_parser import classify_urc
from sim_monitor.modem.driver_base import RawSms
from sim_monitor.modem.fake import FakeModemDriver
from sim_monitor.modem.ota_sms import classify_ota, message_class
from sim_monitor.modem.sms import reassemble_inbound

# SMS-DELIVER from short code 770: PID 0x7F ((U)SIM data download), DCS 0xF6
# (8-bit, class 2), UDH with a 23.048 command-packet IE (0x70).
OTA_UD = "027000" + "00281506192525B00010A1B2C3D4"
OTA_PDU = (
    "00" "44" "03" "81" "77F0" "7F" "F6" "00000000000000"
    + f"{len(bytes.fromhex(OTA_UD)):02X}" + OTA_UD
)
# A normal text ("hi") from +12025550123 for contrast: PID 0, DCS 0.
PLAIN_PDU = "0004" + "0B912120550521F3" + "0000" + "00000000000000" + "02" + "E834"


class TestMessageClass:
    def test_general_group_without_class_bit(self):
        assert message_class(0x00) is None  # default GSM7, no class

    def test_general_group_class2(self):
        assert message_class(0x12) == 2  # class-present bit + class 2

    def test_data_coding_group_class2(self):
        assert message_class(0xF6) == 2  # 1111 group always carries a class

    def test_mwi_groups_have_no_class(self):
        assert message_class(0xC8) is None
        assert message_class(0xE0) is None


class TestClassifyOta:
    def test_pid_sim_data_download(self):
        assert "PID 0x7F" in classify_ota(0x7F, 0x00)

    def test_pid_ansi136(self):
        assert "PID 0x7C" in classify_ota(0x7C, 0x00)

    def test_udh_secured_packets(self):
        assert "UDH 0x70" in classify_ota(0x00, 0x00, (0x70,))
        assert "UDH 0x71" in classify_ota(0x00, 0x00, (0x71,))

    def test_class2_alone(self):
        assert "class 2" in classify_ota(0x00, 0xF6)

    def test_all_markers_join(self):
        reason = classify_ota(0x7F, 0xF6, (0x70,))
        assert "PID 0x7F" in reason and "UDH 0x70" in reason and "class 2" in reason

    def test_plain_text_is_not_ota(self):
        assert classify_ota(0x00, 0x00) is None
        assert classify_ota(0x00, 0x08, (0x00,)) is None  # UCS2 concat text


class TestPduFieldRetention:
    def test_ota_pdu_keeps_protocol_fields(self):
        d = pdu.decode_pdu(OTA_PDU)
        assert d.pid == 0x7F
        assert d.dcs == 0xF6
        assert d.udh_ieis == (0x70,)
        assert d.encoding == "8bit"
        assert d.sender == "770"

    def test_plain_pdu_fields(self):
        d = pdu.decode_pdu(PLAIN_PDU)
        assert d.pid == 0 and d.dcs == 0 and d.udh_ieis == ()
        assert d.text == "hi"


class TestInboxFlagging:
    def test_reassemble_flags_ota_and_keeps_raw_pdu(self):
        rows = reassemble_inbound([RawSms(1, 0, OTA_PDU), RawSms(2, 0, PLAIN_PDU)])
        by_peer = {r["peer"]: r for r in rows}
        ota = by_peer["770"]
        assert ota["ota"] and "PID 0x7F" in ota["ota"]
        assert ota["pid"] == 0x7F and ota["dcs"] == 0xF6
        assert ota["raw_pdu"] == OTA_PDU  # kept verbatim for audit
        plain = by_peer["+12025550123"]
        assert plain["ota"] is None
        assert plain["raw_pdu"] == PLAIN_PDU

    def test_fake_driver_ota_sms_round_trip(self):
        d = FakeModemDriver()
        d.receive_ota_sms()
        rows = reassemble_inbound(d.list_sms())
        assert len(rows) == 1
        assert rows[0]["ota"] and "class 2" in rows[0]["ota"]

    def test_timestampless_pdu_has_stable_identity(self):
        # A zeroed SCTS falls back to "now" for display, but the dedup key must
        # stay constant across repeated inbox reads or the message re-inserts
        # (and re-fires events) on every 15s poll.
        first = reassemble_inbound([RawSms(1, 0, OTA_PDU)])[0]
        second = reassemble_inbound([RawSms(1, 0, OTA_PDU)])[0]
        assert first["dedup"] == second["dedup"]


class TestUrcPduClassification:
    def test_bare_hex_line_is_sms_pdu(self):
        kind, _ = classify_urc(OTA_PDU)
        assert kind == "sms_pdu"

    def test_cmti_still_new_sms(self):
        kind, fields = classify_urc('+CMTI: "ME",3')
        assert kind == "new_sms" and fields["index"] == 3

    def test_short_or_non_hex_lines_stay_unknown(self):
        assert classify_urc("DEADBEEF")[0] == "unknown"          # too short
        assert classify_urc("hello world")[0] == "unknown"
