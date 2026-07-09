"""Pure classification of SIM/eUICC OTA messages. No I/O.

Carrier platforms manage (e)SIMs over SMS-PP "data download" messages: the
network sends a secured packet to the SIM (profile enable/disable/delete,
RAM/RFM file updates, eUICC SM-SR traffic per GSMA SGP.02), and the SIM may
answer with a proof-of-receipt. These are recognisable from three protocol
fields, all preserved by our PDU decoder:

- TP-PID 0x7F = (U)SIM data download (3GPP TS 23.040 §9.2.3.9); 0x7C =
  ANSI-136 R-DATA, used the same way on some networks.
- TP-DCS message class 2 = "(U)SIM specific message" (TS 23.038 §4) — the ME
  must pass it to the SIM.
- UDH information elements 0x70 (command packet) / 0x71 (response packet)
  mark a TS 23.048 / ETSI TS 102 225 secured packet (SCP80).

Any one of these means the message is SIM-directed OTA rather than a human
text. classify_ota returns a human-readable reason string, or None.
"""

from __future__ import annotations

PID_SIM_DATA_DOWNLOAD = 0x7F
PID_ANSI136_RDATA = 0x7C
UDH_COMMAND_PACKET = 0x70
UDH_RESPONSE_PACKET = 0x71


def message_class(dcs: int) -> int | None:
    """The TP-DCS message class (0-3), or None when the DCS carries no class.
    Class 2 is '(U)SIM specific'. Handles the general-coding groups (00xx/01xx,
    class present only when bit 4 is set) and the 1111 data-coding group
    (class always present)."""
    group = dcs >> 4
    if group <= 0x7:  # general data coding / auto-deletion groups
        return dcs & 0x03 if dcs & 0x10 else None
    if group == 0xF:  # data coding + message class group
        return dcs & 0x03
    return None


def classify_ota(pid: int, dcs: int, udh_ieis: tuple[int, ...] = ()) -> str | None:
    """Reason string when the message is SIM/eUICC OTA traffic, else None."""
    reasons: list[str] = []
    if pid == PID_SIM_DATA_DOWNLOAD:
        reasons.append("(U)SIM data download (PID 0x7F)")
    elif pid == PID_ANSI136_RDATA:
        reasons.append("ANSI-136 R-DATA (PID 0x7C)")
    if UDH_COMMAND_PACKET in udh_ieis:
        reasons.append("secured command packet (UDH 0x70)")
    if UDH_RESPONSE_PACKET in udh_ieis:
        reasons.append("secured response packet (UDH 0x71)")
    if message_class(dcs) == 2:
        reasons.append("message class 2 (SIM-specific)")
    return "; ".join(reasons) if reasons else None
