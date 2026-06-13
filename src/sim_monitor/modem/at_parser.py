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


def parse_imei(lines: list[str]) -> str:
    """AT+CGSN answers with a bare 14-17 digit IMEI/IMEISV."""
    for line in lines:
        line = line.strip().strip('"')
        if re.fullmatch(r"\d{14,17}", line):
            return line
    raise ATParseError(f"no IMEI in response: {lines!r}")


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


# ── URC classification ──────────────────────────────────────────────────────
# Map an unsolicited result code line to a (kind, fields) pair. Pure; the driver
# wraps the result in a UrcEvent and the daemon reacts (fetch SMS, re-evaluate
# the SIM after a refresh, record registration changes, etc.).

# 3GPP +CxREG registration state -> human label.
_REG_STATE = {
    0: "not-registered",
    1: "registered-home",
    2: "searching",
    3: "denied",
    4: "unknown",
    5: "registered-roaming",
}


def registration_label(stat: int) -> str:
    return _REG_STATE.get(stat, f"state-{stat}")


def classify_urc(line: str) -> tuple[str, dict]:
    """Classify one unsolicited line. Returns (kind, fields).

    kinds: new_sms, sms_deliver, sim_status, registration, nitz, ring,
    no_carrier, unknown.
    """
    line = line.strip()
    upper = line.upper()

    # New SMS stored on the modem: +CMTI: "ME",3
    m = re.match(r'\+CMTI:\s*"?([A-Z]+)"?\s*,\s*(\d+)', line, re.I)
    if m:
        return "new_sms", {"storage": m.group(1).upper(), "index": int(m.group(2))}

    # Directly-delivered SMS (+CMT) — header line; body follows on next line.
    if upper.startswith("+CMT:") or upper.startswith("+CDS:"):
        return "sms_deliver", {"header": line}

    # Quectel SIM insertion / status change (a SIM refresh shows up here).
    m = re.match(r"\+QSIMSTAT:\s*(\d+)\s*,\s*(\d+)", line)
    if m:
        return "sim_status", {"enabled": int(m.group(1)), "inserted": int(m.group(2))}
    if upper.startswith("+QUSIM:"):
        return "sim_status", {"raw": line}

    # Registration change: +CEREG: <stat>[,...] / +CREG: / +CGREG:
    m = re.match(r"\+(CREG|CGREG|CEREG):\s*(\d+)(?:\s*,\s*\d+)?\s*$", line, re.I)
    if m:
        # URC form is "+CEREG: <stat>"; the n,<stat> solicited form is handled
        # by parse_cereg. A trailing ,<n> (no stat) is also just <stat>.
        return "registration", {
            "domain": m.group(1).upper(),
            "stat": int(m.group(2)),
            "label": registration_label(int(m.group(2))),
        }
    # Verbose URC form with location: +CEREG: <stat>,"<tac>","<ci>",<act>
    m = re.match(
        r'\+(CREG|CGREG|CEREG):\s*(\d+)\s*,\s*"?([0-9A-Fa-f]*)"?\s*,\s*"?([0-9A-Fa-f]*)"?',
        line,
    )
    if m and upper.count(",") >= 2:
        return "registration", {
            "domain": m.group(1).upper(),
            "stat": int(m.group(2)),
            "label": registration_label(int(m.group(2))),
            "tac": m.group(3) or None,
            "ci": m.group(4) or None,
        }

    if upper.startswith(("+CTZV:", "+CTZE:", "+CTZDST:", "*PSUTTZ", "+QLTS")):
        return "nitz", {"raw": line}
    if upper == "RING":
        return "ring", {}
    if upper.startswith("NO CARRIER"):
        return "no_carrier", {}
    return "unknown", {"raw": line}


def parse_qcsq(lines: list[str]) -> dict | None:
    """Quectel +QCSQ: "<sysmode>",<rssi>,<rsrp>,<sinr>,<rsrq> (LTE), or
    +QCSQ: "NOSERVICE". Values are dBm (sinr is Quectel's raw index)."""
    for line in lines:
        m = re.match(r'\+QCSQ:\s*"([^"]*)"(?:\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,'
                     r'\s*(-?\d+)\s*,\s*(-?\d+))?', line)
        if m:
            if m.group(2) is None:
                return {"rat": m.group(1), "rssi": None, "rsrp": None,
                        "sinr": None, "rsrq": None}
            return {
                "rat": m.group(1),
                "rssi": int(m.group(2)),
                "rsrp": int(m.group(3)),
                "sinr": int(m.group(4)),
                "rsrq": int(m.group(5)),
            }
    return None


def parse_qeng_servingcell(lines: list[str]) -> dict | None:
    """Quectel AT+QENG="servingcell" for LTE. Tolerant token parse."""
    for line in lines:
        if not line.startswith("+QENG:") or "servingcell" not in line:
            continue
        body = line.split(":", 1)[1]
        toks = [t.strip().strip('"') for t in body.split(",")]
        # toks: servingcell,state,LTE,FDD,MCC,MNC,cellID,PCID,EARFCN,band,
        #       ULbw,DLbw,TAC,RSRP,RSRQ,RSSI,SINR,...
        if len(toks) < 17 or toks[2].upper() != "LTE":
            return {"rat": toks[2] if len(toks) > 2 else None, "state": toks[1]
                    if len(toks) > 1 else None}

        def _int(s, base=10):
            try:
                return int(s, base)
            except (ValueError, TypeError):
                return None

        return {
            "rat": "LTE",
            "state": toks[1],
            "mcc": _int(toks[4]),
            "mnc": _int(toks[5]),
            "cell_id": toks[6],
            "pci": _int(toks[7]),
            "earfcn": _int(toks[8]),
            "band": _int(toks[9]),
            "tac": toks[12],
            "rsrp": _int(toks[13]),
            "rsrq": _int(toks[14]),
            "rssi": _int(toks[15]),
            "sinr": _int(toks[16]),
        }
    return None


def parse_qnwinfo(lines: list[str]) -> dict | None:
    """Quectel +QNWINFO: "<act>","<oper>","<band>",<channel>."""
    for line in lines:
        if "No Service" in line:
            return {"act": None, "operator_numeric": None, "band": None, "channel": None}
        m = re.match(
            r'\+QNWINFO:\s*"([^"]*)"\s*,\s*"?([^",]*)"?\s*,\s*"([^"]*)"\s*,\s*(\d+)', line
        )
        if m:
            return {
                "act": m.group(1),
                "operator_numeric": m.group(2),
                "band": m.group(3),
                "channel": int(m.group(4)),
            }
    return None


def parse_cmgl(lines: list[str]) -> list[tuple[int, int, str]]:
    """Parse AT+CMGL (PDU mode): header lines '+CMGL: idx,stat,alpha,len'
    each followed by a PDU hex line. Returns [(index, status, pdu_hex)]."""
    out = []
    pending: tuple[int, int] | None = None
    for line in lines:
        m = re.match(r"\+CMGL:\s*(\d+)\s*,\s*(\d+)\s*,", line)
        if m:
            pending = (int(m.group(1)), int(m.group(2)))
            continue
        if pending and re.fullmatch(r"[0-9A-Fa-f]+", line.strip()):
            out.append((pending[0], pending[1], line.strip().upper()))
            pending = None
    return out


def parse_cmgs(lines: list[str]) -> int | None:
    """Parse the +CMGS: <mr> message-reference returned after sending."""
    for line in lines:
        m = re.match(r"\+CMGS:\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def parse_cereg(lines: list[str]) -> dict | None:
    """Parse a solicited AT+CEREG? reply: +CEREG: <n>,<stat>[,"<tac>","<ci>"...]."""
    for line in lines:
        m = re.match(
            r'\+CEREG:\s*\d+\s*,\s*(\d+)(?:\s*,\s*"?([0-9A-Fa-f]*)"?\s*,\s*"?([0-9A-Fa-f]*)"?)?',
            line,
        )
        if m:
            return {
                "stat": int(m.group(1)),
                "label": registration_label(int(m.group(1))),
                "tac": m.group(2) or None,
                "ci": m.group(3) or None,
            }
    return None
