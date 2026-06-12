"""Pure parsers for AT command responses (no I/O).

Each function takes the response payload lines (final OK/ERROR already stripped
by the AT channel) and returns structured data. Vendors differ slightly in
prefixes (+CCID vs +QCCID vs #CCID), so parsers accept all known variants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class ATParseError(ValueError):
    pass


_ICCID_PREFIXES = ("+CCID:", "+QCCID:", "#CCID:", "+ICCID:")


def parse_iccid(lines: list[str]) -> str:
    """Extract the ICCID from CCID-style responses (prefixed or bare number)."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        for prefix in _ICCID_PREFIXES:
            if line.upper().startswith(prefix):
                value = line[len(prefix):].strip().strip('"')
                if value:
                    return value
        # Some modems answer with the bare number (possibly with F filler).
        if re.fullmatch(r"\d{18,20}[Ff]?", line):
            return line
    raise ATParseError(f"no ICCID in response: {lines!r}")


def parse_imsi(lines: list[str]) -> str:
    """AT+CIMI answers with a bare 14-15 digit IMSI."""
    for line in lines:
        line = line.strip()
        if re.fullmatch(r"\d{14,15}", line):
            return line
    raise ATParseError(f"no IMSI in response: {lines!r}")


@dataclass(frozen=True)
class SignalQuality:
    rssi_dbm: int | None  # None = unknown (modem reported 99)
    percent: int | None


def parse_csq(lines: list[str]) -> SignalQuality:
    """+CSQ: <rssi 0-31|99>,<ber> — rssi maps to dBm as -113 + 2*n."""
    for line in lines:
        m = re.match(r"\s*\+CSQ:\s*(\d+)\s*,\s*\d+", line)
        if m:
            n = int(m.group(1))
            if n == 99 or n > 31:
                return SignalQuality(None, None)
            return SignalQuality(-113 + 2 * n, round(n / 31 * 100))
    raise ATParseError(f"no +CSQ in response: {lines!r}")


def parse_cops(lines: list[str]) -> str | None:
    """+COPS: <mode>[,<format>,"<operator>"[,<act>]] — operator name or None."""
    for line in lines:
        m = re.match(r'\s*\+COPS:\s*\d+(?:\s*,\s*\d+\s*,\s*"([^"]*)")?', line)
        if m:
            return m.group(1) or None
    raise ATParseError(f"no +COPS in response: {lines!r}")


def parse_cpin(lines: list[str]) -> str:
    """+CPIN: READY | SIM PIN | SIM PUK | ... (raises if SIM is absent: CME ERROR
    is handled at the channel level, this only sees a payload)."""
    for line in lines:
        m = re.match(r"\s*\+CPIN:\s*(.+?)\s*$", line)
        if m:
            return m.group(1)
    raise ATParseError(f"no +CPIN in response: {lines!r}")


def parse_cgmi(lines: list[str]) -> str:
    """Manufacturer string: first non-empty, non-echo payload line."""
    for line in lines:
        line = line.strip()
        if line and not line.startswith(("AT", "+")):
            return line
    raise ATParseError(f"no manufacturer in response: {lines!r}")


# AT+CGDCONT? PDP type strings <-> our schema names
_PDP_TYPE_MAP = {"IP": "IPv4", "IPV6": "IPv6", "IPV4V6": "IPv4v6"}
_PDP_TYPE_TO_AT = {"IPv4": "IP", "IPv6": "IPV6", "IPv4v6": "IPV4V6"}


@dataclass(frozen=True)
class ActualPdpContext:
    """A PDP context as currently defined on the modem."""

    cid: int
    pdp_type: str  # IPv4 | IPv6 | IPv4v6 | raw string for exotic types
    apn: str


def parse_cgdcont(lines: list[str]) -> list[ActualPdpContext]:
    """Parse AT+CGDCONT? output: +CGDCONT: <cid>,"<type>","<apn>",..."""
    contexts = []
    for line in lines:
        m = re.match(r'\s*\+CGDCONT:\s*(\d+)\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"', line)
        if m:
            cid = int(m.group(1))
            raw_type = m.group(2).upper()
            contexts.append(
                ActualPdpContext(
                    cid=cid,
                    pdp_type=_PDP_TYPE_MAP.get(raw_type, m.group(2)),
                    apn=m.group(3),
                )
            )
    return contexts


def pdp_type_to_at(pdp_type: str) -> str:
    """Map a schema pdp_type (IPv4/IPv6/IPv4v6) to the AT+CGDCONT string."""
    return _PDP_TYPE_TO_AT[pdp_type]
