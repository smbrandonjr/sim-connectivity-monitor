from sim_monitor.modem import pdu
from sim_monitor.modem.driver_base import RawSms
from sim_monitor.modem.sms import reassemble_inbound


def deliver_pdu(sender, text):
    """Build a DELIVER PDU from a SUBMIT body (mirrors fake.receive_sms)."""
    submit_hex, _ = pdu.encode_submit(sender, text)[0]
    b = bytes.fromhex(submit_hex)
    oa_octets = 1 + (b[3] + 1) // 2
    idx = 4 + oa_octets + 2
    dcs = b[4 + oa_octets + 1]
    udl = b[idx]
    ud = b[idx + 1:]
    oa = b[3:4 + oa_octets].hex().upper()
    return ("0004" + oa + "00" + f"{dcs:02X}" + "00000000000000"
            + f"{udl:02X}" + ud.hex().upper())


class TestReassemble:
    def test_single_message(self):
        raw = [RawSms(1, 0, deliver_pdu("+12025550123", "hello world"))]
        rows = reassemble_inbound(raw)
        assert len(rows) == 1
        assert rows[0]["body"] == "hello world"
        assert rows[0]["status"] == "unread"
        assert rows[0]["modem_indices"] == [1]

    def test_read_status(self):
        raw = [RawSms(1, 1, deliver_pdu("+12025550123", "hi"))]
        assert reassemble_inbound(raw)[0]["status"] == "read"

    def test_undecodable_skipped(self):
        raw = [RawSms(1, 0, "ZZZZ"), RawSms(2, 0, deliver_pdu("+1", "ok"))]
        rows = reassemble_inbound(raw)
        assert len(rows) == 1
        assert rows[0]["body"] == "ok"

    def test_binary_message_shown_as_hex(self):
        # 8-bit DCS deliver with body "DEADBEEF"
        ud = "DEADBEEF"
        udl = len(bytes.fromhex(ud))
        p = "0004" + "0181F0" + "00" + "04" + "00000000000000" + f"{udl:02X}" + ud
        rows = reassemble_inbound([RawSms(1, 0, p)])
        assert rows[0]["encoding"] == "8bit"
        assert rows[0]["body"] == "DEADBEEF"


class TestFakeDriverSms:
    def test_receive_and_list(self):
        from sim_monitor.modem.fake import FakeModemDriver

        d = FakeModemDriver()
        idx = d.receive_sms("+12025550123", "ping")
        raw = d.list_sms()
        assert len(raw) == 1 and raw[0].index == idx
        rows = reassemble_inbound(raw)
        assert rows[0]["body"] == "ping"
        # receiving queued a +CMTI URC
        events = d.poll_events()
        assert events and events[0].kind == "new_sms"

    def test_send_and_delete(self):
        from sim_monitor.modem.fake import FakeModemDriver

        d = FakeModemDriver()
        parts = d.send_sms("+12025550123", "hi there")
        assert parts == 1
        assert d.sent_log == [("+12025550123", "hi there")]
        idx = d.receive_sms("+1", "x")
        d.delete_sms(idx)
        assert d.list_sms() == []
