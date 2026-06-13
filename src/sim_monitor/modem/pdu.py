"""Pure SMS PDU codec (GSM 03.40 / 03.38). No I/O.

Decoding handles SMS-DELIVER PDUs as returned by AT+CMGL/AT+CMGR in PDU mode:
GSM 7-bit, UCS2 (Unicode), and 8-bit binary data coding; concatenation headers
(UDH) are parsed so multi-part messages can be reassembled. Encoding builds
SMS-SUBMIT PDUs for AT+CMGS, splitting long messages into concatenated parts.

Why PDU mode (not text mode): it is the honest view of what the modem received
— Unicode, multipart, and binary/OTA (class-2) messages all survive intact,
which matters for understanding carrier/OTA traffic.
"""

from __future__ import annotations

from dataclasses import dataclass

# GSM 03.38 default alphabet (basic table); index 0x1B is the escape to the
# extension table.
GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)
GSM7_EXT = {
    0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\",
    0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "€",
}
_GSM7_BASIC_REV = {c: i for i, c in enumerate(GSM7_BASIC) if i != 0x1B}
_GSM7_EXT_REV = {c: k for k, c in GSM7_EXT.items()}


@dataclass(frozen=True)
class Concat:
    ref: int
    total: int
    seq: int


@dataclass(frozen=True)
class DecodedSms:
    sender: str
    timestamp: str          # ISO-ish "YYYY-MM-DD HH:MM:SS" (+tz omitted)
    text: str               # decoded text; for 8-bit binary, hex string
    encoding: str           # "gsm7" | "ucs2" | "8bit"
    concat: Concat | None    # set when part of a multi-part message


# ── helpers ─────────────────────────────────────────────────────────────────


def _swap_semi_octets(hex_digits: str) -> str:
    out = []
    for i in range(0, len(hex_digits) - 1, 2):
        out.append(hex_digits[i + 1])
        out.append(hex_digits[i])
    if len(hex_digits) % 2:
        out.append(hex_digits[-1])
    return "".join(out)


def _decode_address(octets: bytes, length_digits: int) -> str:
    """Decode a TP-address: type-of-address octet then BCD digits."""
    toa = octets[0]
    number = octets[1:]
    if (toa & 0x70) == 0x50:  # alphanumeric address (GSM7-packed)
        septets = (length_digits * 4) // 7
        return _unpack_gsm7(number, septets, 0)
    digits = _swap_semi_octets(number.hex().upper())[:length_digits]
    digits = digits.replace("F", "")
    prefix = "+" if (toa & 0x70) == 0x10 else ""  # international
    return prefix + digits


def _decode_scts(octets: bytes) -> str:
    d = _swap_semi_octets(octets[:7].hex())
    yy, mm, dd, hh, mi, ss = (d[0:2], d[2:4], d[4:6], d[6:8], d[8:10], d[10:12])
    return f"20{yy}-{mm}-{dd} {hh}:{mi}:{ss}"


def _unpack_gsm7(data: bytes, septet_count: int, skip_bits: int = 0) -> str:
    """Unpack packed 7-bit septets, then map via the default+extension tables."""
    bits = 0
    nbits = 0
    septets: list[int] = []
    for byte in data:
        bits |= byte << nbits
        nbits += 8
        while nbits >= 7:
            septets.append(bits & 0x7F)
            bits >>= 7
            nbits -= 7
    # Drop alignment fill septets (after a UDH) and trim to count.
    skip_septets = skip_bits // 7
    septets = septets[skip_septets:skip_septets + septet_count]
    out = []
    i = 0
    while i < len(septets):
        code = septets[i]
        if code == 0x1B and i + 1 < len(septets):
            i += 1
            out.append(GSM7_EXT.get(septets[i], " "))
        else:
            out.append(GSM7_BASIC[code] if code < len(GSM7_BASIC) else "?")
        i += 1
    return "".join(out)


def _pack_gsm7(septets: list[int]) -> bytes:
    out = bytearray()
    bits = 0
    nbits = 0
    for s in septets:
        bits |= s << nbits
        nbits += 7
        while nbits >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8
    if nbits:
        out.append(bits & 0xFF)
    return bytes(out)


def _text_to_gsm7_septets(text: str) -> list[int] | None:
    """Return septet codes, or None if text needs UCS2 (a char is unmappable)."""
    septets: list[int] = []
    for ch in text:
        if ch in _GSM7_BASIC_REV:
            septets.append(_GSM7_BASIC_REV[ch])
        elif ch in _GSM7_EXT_REV:
            septets.append(0x1B)
            septets.append(_GSM7_EXT_REV[ch])
        else:
            return None
    return septets


def _parse_udh_concat(ud: bytes) -> tuple[int, Concat | None]:
    """Parse a UDH at the start of UD. Returns (udh_total_len_octets, Concat)."""
    udhl = ud[0]
    concat = None
    i = 1
    end = 1 + udhl
    while i + 1 < end:
        iei, ielen = ud[i], ud[i + 1]
        val = ud[i + 2:i + 2 + ielen]
        if iei == 0x00 and ielen == 3:
            concat = Concat(ref=val[0], total=val[1], seq=val[2])
        elif iei == 0x08 and ielen == 4:
            concat = Concat(ref=(val[0] << 8) | val[1], total=val[2], seq=val[3])
        i += 2 + ielen
    return 1 + udhl, concat


# ── decode ──────────────────────────────────────────────────────────────────


def decode_pdu(pdu_hex: str) -> DecodedSms:
    """Decode a received SMS-DELIVER PDU (hex string)."""
    b = bytes.fromhex(pdu_hex.strip())
    smsc_len = b[0]
    idx = 1 + smsc_len  # skip SMSC field
    first = b[idx]
    udhi = bool(first & 0x40)
    oa_len = b[idx + 1]
    oa_octets = 1 + (oa_len + 1) // 2  # TOA + BCD digits
    sender = _decode_address(b[idx + 2:idx + 2 + oa_octets], oa_len)
    idx = idx + 2 + oa_octets
    idx += 1  # TP-PID
    dcs = b[idx]
    scts = _decode_scts(b[idx + 1:idx + 8])
    udl = b[idx + 8]
    ud = b[idx + 9:]

    udh_len = 0
    concat = None
    if udhi:
        udh_len, concat = _parse_udh_concat(ud)

    # Data coding: bits 2-3 select alphabet (0=GSM7, 1=8bit, 2=UCS2).
    alphabet = (dcs >> 2) & 0x03
    if alphabet == 2:  # UCS2
        text = ud[udh_len:].decode("utf-16-be", errors="replace")
        encoding = "ucs2"
    elif alphabet == 1:  # 8-bit binary
        text = ud[udh_len:].hex().upper()
        encoding = "8bit"
    else:  # GSM 7-bit
        fill_bits = (7 - (udh_len * 8) % 7) % 7 if udh_len else 0
        septet_count = udl - (udh_len * 8 + fill_bits) // 7
        text = _unpack_gsm7(ud, septet_count, skip_bits=udh_len * 8 + fill_bits)
        encoding = "gsm7"
    return DecodedSms(sender, scts, text, encoding, concat)


# ── encode ──────────────────────────────────────────────────────────────────


def _encode_address(number: str) -> bytes:
    intl = number.startswith("+")
    digits = number.lstrip("+")
    toa = 0x91 if intl else 0x81
    bcd = _swap_semi_octets(digits if len(digits) % 2 == 0 else digits + "F")
    return bytes([len(digits), toa]) + bytes.fromhex(bcd)


def encode_submit(number: str, text: str, *, ref: int = 0) -> list[tuple[str, int]]:
    """Build SMS-SUBMIT PDU(s) for AT+CMGS. Returns [(pdu_hex, tpdu_len_octets)].

    Long messages are split into concatenated parts (8-bit reference UDH).
    `tpdu_len_octets` is the AT+CMGS length argument (octets after the SMSC field).
    """
    septets = _text_to_gsm7_septets(text)
    da = _encode_address(number)

    if septets is not None:
        return _encode_parts_gsm7(da, septets, ref)
    return _encode_parts_ucs2(da, text, ref)


def _assemble(da: bytes, dcs: int, udl: int, ud: bytes, udhi: bool) -> tuple[str, int]:
    first = 0x41 if udhi else 0x01  # SMS-SUBMIT, no VP (UDHI bit if concatenated)
    tpdu = bytes([first, 0x00]) + da + bytes([0x00, dcs, udl]) + ud
    return ("00" + tpdu.hex().upper(), len(tpdu))


def _concat_udh(ref: int, total: int, seq: int) -> bytes:
    return bytes([0x05, 0x00, 0x03, ref & 0xFF, total, seq])


def _encode_parts_gsm7(da: bytes, septets: list[int], ref: int) -> list[tuple[str, int]]:
    if len(septets) <= 160:
        ud = _pack_gsm7(septets)
        return [_assemble(da, 0x00, len(septets), ud, udhi=False)]
    # 153 septets/part leaves room for a 6-octet (7-septet) UDH + 1 fill bit.
    chunks = [septets[i:i + 153] for i in range(0, len(septets), 153)]
    parts = []
    for seq, chunk in enumerate(chunks, start=1):
        udh = _concat_udh(ref, len(chunks), seq)
        # 1 fill bit so the text starts on a septet boundary after the 6-octet UDH.
        ud = udh + _pack_gsm7_with_offset(chunk, 1)
        udl = (len(udh) * 8 + 1) // 7 + len(chunk)
        parts.append(_assemble(da, 0x00, udl, ud, udhi=True))
    return parts


def _pack_gsm7_with_offset(septets: list[int], fill_bits: int) -> bytes:
    out = bytearray()
    bits = 0
    nbits = fill_bits
    for s in septets:
        bits |= s << nbits
        nbits += 7
        while nbits >= 8:
            out.append(bits & 0xFF)
            bits >>= 8
            nbits -= 8
    if nbits:
        out.append(bits & 0xFF)
    return bytes(out)


def _encode_parts_ucs2(da: bytes, text: str, ref: int) -> list[tuple[str, int]]:
    raw = text.encode("utf-16-be")
    if len(raw) <= 140:
        return [_assemble(da, 0x08, len(raw), raw, udhi=False)]
    chunks = [raw[i:i + 134] for i in range(0, len(raw), 134)]  # 140-6 UDH
    parts = []
    for seq, chunk in enumerate(chunks, start=1):
        udh = _concat_udh(ref, len(chunks), seq)
        ud = udh + chunk
        parts.append(_assemble(da, 0x08, len(ud), ud, udhi=True))
    return parts
