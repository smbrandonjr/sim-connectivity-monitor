from sim_monitor.modem.pdu import (
    decode_pdu,
    encode_submit,
)


class TestDecode7bit:
    def test_classic_hellohello(self):
        # Well-known reference PDU: sender +447xxx..., text "hellohello".
        pdu = "0791447779071413040B914477676767670000991121017455400AE8329BFD4697D9EC37"
        sms = decode_pdu(pdu)
        assert sms.encoding == "gsm7"
        assert sms.text == "hellohello"
        assert sms.sender == "+44777676767"  # 11 digits, semi-octet swapped
        assert sms.concat is None

    def test_timestamp_decoded(self):
        pdu = "0791447779071413040B914477676767670000991121017455400AE8329BFD4697D9EC37"
        sms = decode_pdu(pdu)
        assert sms.timestamp.startswith("2099-11-12")  # yy=99 from this fixture


class TestTimestampEpoch:
    def _deliver(self, scts_hex: str) -> str:
        # Minimal SMS-DELIVER, empty GSM7 body, with the given 7-octet SCTS.
        return "0004" + "0181F0" + "00" + "00" + scts_hex + "00"

    def test_tz_offset_resolves_to_utc(self):
        import calendar

        # SCTS wall time 2026-06-24 22:42:46 at +2h (8 quarter-hours).
        # Transmitted octets are semi-octet swapped; tz "+08" -> octet "80".
        sms = decode_pdu(self._deliver("62604222246480"))
        assert sms.timestamp == "2026-06-24 22:42:46"  # SMSC local wall time
        # The true instant is two hours earlier in UTC.
        assert sms.timestamp_epoch == calendar.timegm((2026, 6, 24, 20, 42, 46, 0, 0, 0))

    def test_negative_tz_offset(self):
        import calendar

        # Same wall time at -5h (Eastern, 20 quarter-hours). After the semi-octet
        # swap the tz reads "A0" (sign bit 0x80 set, value 20), so the octet is
        # transmitted as "0A".
        sms = decode_pdu(self._deliver("6260422224640A"))
        assert sms.timestamp == "2026-06-24 22:42:46"
        assert sms.timestamp_epoch == calendar.timegm((2026, 6, 25, 3, 42, 46, 0, 0, 0))

    def test_zeroed_scts_has_no_epoch(self):
        sms = decode_pdu(self._deliver("00000000000000"))
        assert sms.timestamp_epoch is None


class TestDecodeUcs2:
    def test_unicode(self):
        # DCS 0x08 UCS2, text "Hi😀" would need surrogate; use a BMP example "Héllo".
        # Build a minimal deliver PDU by hand: SMSC 00, first 04 (deliver, no UDH),
        # OA len 01 type 81 digit "1"(F), PID 00 DCS 08, SCTS, UDL, UD UTF-16BE.
        text = "Héllo"
        ud = text.encode("utf-16-be").hex().upper()
        udl = len(text.encode("utf-16-be"))
        pdu = "0004" + "01" + "81" + "F0" + "00" + "08" + "00000000000000" + f"{udl:02X}" + ud
        sms = decode_pdu(pdu)
        assert sms.encoding == "ucs2"
        assert sms.text == "Héllo"


class TestDecodeMultipart:
    def test_concat_header_parsed(self):
        # 8-bit data with a UDH concat IE (IEI 00): ref=0x42 total=2 seq=1, body "AB".
        udh = "050003420201"  # UDHL=05, IEI=00 len=03 ref=42 total=02 seq=01
        body = "4142"
        ud = udh + body
        udl = len(bytes.fromhex(ud))
        pdu = "0044" + "01" + "81" + "F0" + "00" + "04" + "00000000000000" + f"{udl:02X}" + ud
        sms = decode_pdu(pdu)
        assert sms.concat is not None
        assert (sms.concat.ref, sms.concat.total, sms.concat.seq) == (0x42, 2, 1)
        assert sms.encoding == "8bit"
        assert sms.text == "4142"


class TestEncodeSubmit:
    def test_short_gsm7_single_part(self):
        parts = encode_submit("+12025550123", "Test message")
        assert len(parts) == 1
        pdu, tlen = parts[0]
        assert pdu.startswith("00")          # SMSC length 00 (use modem default)
        assert pdu[2:4] == "01"              # SMS-SUBMIT, no VP, no UDH
        assert tlen == len(bytes.fromhex(pdu)) - 1  # length excludes SMSC octet

    def test_roundtrip_gsm7_via_deliver_shape(self):
        # Encode a submit, then verify the packed user data decodes back by
        # wrapping the same UD into a DELIVER and decoding.
        parts = encode_submit("+12025550123", "hellohello")
        pdu, _ = parts[0]
        b = bytes.fromhex(pdu)
        # SUBMIT layout: 00|01|MR|DA...|PID|DCS|UDL|UD
        # Find UD by skipping: smsc(1)+first(1)+mr(1)+da
        da_len_digits = b[3]
        da_octets = 1 + (da_len_digits + 1) // 2
        idx = 4 + da_octets  # after DA (b[3] is len, then TOA+bcd)
        idx += 2  # PID + DCS
        udl = b[idx]
        ud = b[idx + 1:]
        deliver = (
            "0004" + "0181F0" + "00" + "00" + "00000000000000"
            + f"{udl:02X}" + ud.hex().upper()
        )
        assert decode_pdu(deliver).text == "hellohello"

    def test_unicode_uses_ucs2(self):
        parts = encode_submit("+12025550123", "café ☕")
        pdu, _ = parts[0]
        b = bytes.fromhex(pdu)
        da_octets = 1 + (b[3] + 1) // 2
        dcs = b[4 + da_octets + 1]
        assert dcs == 0x08  # UCS2

    def test_long_gsm7_splits_into_concatenated_parts(self):
        parts = encode_submit("+12025550123", "x" * 200)
        assert len(parts) == 2
        for pdu, _ in parts:
            assert pdu[2:4] == "41"  # UDHI bit set on each concatenated part

    def test_each_part_decodes_back(self):
        msg = "A" * 320  # 3 parts
        parts = encode_submit("+12025550123", msg, ref=7)
        recovered = ""
        for pdu, _ in parts:
            b = bytes.fromhex(pdu)
            da_octets = 1 + (b[3] + 1) // 2
            idx = 4 + da_octets + 2
            udl = b[idx]
            ud = b[idx + 1:]
            deliver = (
                "0044" + "0181F0" + "00" + "00" + "00000000000000"
                + f"{udl:02X}" + ud.hex().upper()
            )
            sms = decode_pdu(deliver)
            assert sms.concat.total == 3
            recovered += sms.text
        assert recovered == msg
