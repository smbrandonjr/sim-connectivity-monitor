"""Pure reassembly of stored SMS into inbox rows.

Decodes each raw PDU and stitches multi-part (concatenated) messages back
together by their UDH reference, producing one row per logical message ready
for storage/display. No I/O.
"""

from __future__ import annotations

import time

from sim_monitor.modem import pdu
from sim_monitor.modem.driver_base import RawSms

_STATUS = {0: "unread", 1: "read", 2: "unread", 3: "read"}


def reassemble_inbound(raw_list: list[RawSms]) -> list[dict]:
    """Group raw PDUs into logical messages. Returns dicts shaped for db.sms."""
    singles: list[dict] = []
    groups: dict[tuple[str, int], list[tuple[int, int, pdu.DecodedSms]]] = {}

    for raw in raw_list:
        try:
            decoded = pdu.decode_pdu(raw.pdu_hex)
        except (ValueError, IndexError):
            continue  # skip undecodable rows rather than crash the inbox
        if decoded.concat is None:
            singles.append(_row([raw.index], raw.status, decoded, decoded.text, parts=1))
        else:
            key = (decoded.sender, decoded.concat.ref)
            groups.setdefault(key, []).append((raw.index, raw.status, decoded))

    rows = singles
    for parts in groups.values():
        parts.sort(key=lambda p: p[2].concat.seq)
        indices = [idx for idx, _, _ in parts]
        worst_status = min(st for _, st, _ in parts)  # 0(unread) wins
        body = "".join(d.text for _, _, d in parts)
        head = parts[0][2]
        rows.append(_row(indices, worst_status, head, body, parts=head.concat.total))

    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def _row(indices: list[int], status: int, decoded: pdu.DecodedSms, body: str, parts: int) -> dict:
    return {
        "ts": _ts_to_epoch(decoded.timestamp),
        "peer": decoded.sender,
        "body": body,
        "encoding": decoded.encoding,
        "status": _STATUS.get(status, "read"),
        "modem_indices": indices,
        "parts": parts,
        "raw_pdu": None,
    }


def _ts_to_epoch(scts: str) -> float:
    try:
        return time.mktime(time.strptime(scts, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return time.time()
