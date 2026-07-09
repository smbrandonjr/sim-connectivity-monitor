"""Pure reassembly of stored SMS into inbox rows.

Decodes each raw PDU and stitches multi-part (concatenated) messages back
together by their UDH reference, producing one row per logical message ready
for storage/display. No I/O.
"""

from __future__ import annotations

import hashlib
import time

from sim_monitor.modem import pdu
from sim_monitor.modem.driver_base import RawSms
from sim_monitor.modem.ota_sms import classify_ota

_STATUS = {0: "unread", 1: "read", 2: "unread", 3: "read"}


def reassemble_inbound(raw_list: list[RawSms]) -> list[dict]:
    """Group raw PDUs into logical messages. Returns dicts shaped for db.sms."""
    singles: list[dict] = []
    groups: dict[tuple[str, int], list[tuple[RawSms, pdu.DecodedSms]]] = {}

    for raw in raw_list:
        try:
            decoded = pdu.decode_pdu(raw.pdu_hex)
        except (ValueError, IndexError):
            continue  # skip undecodable rows rather than crash the inbox
        if decoded.concat is None:
            singles.append(_row(
                [raw.index], raw.status, decoded, decoded.text,
                parts=1, pdus=[raw.pdu_hex], udh_ieis=decoded.udh_ieis,
            ))
        else:
            key = (decoded.sender, decoded.concat.ref)
            groups.setdefault(key, []).append((raw, decoded))

    rows = singles
    for parts in groups.values():
        parts.sort(key=lambda p: p[1].concat.seq)
        indices = [raw.index for raw, _ in parts]
        worst_status = min(raw.status for raw, _ in parts)  # 0(unread) wins
        body = "".join(d.text for _, d in parts)
        head = parts[0][1]
        # OTA markers may sit in any part's UDH; classify over the union.
        ieis = tuple({iei for _, d in parts for iei in d.udh_ieis})
        rows.append(_row(
            indices, worst_status, head, body, parts=head.concat.total,
            pdus=[raw.pdu_hex for raw, _ in parts], udh_ieis=ieis,
        ))

    rows.sort(key=lambda r: r["ts"], reverse=True)
    return rows


def _row(
    indices: list[int],
    status: int,
    decoded: pdu.DecodedSms,
    body: str,
    parts: int,
    pdus: list[str],
    udh_ieis: tuple[int, ...],
) -> dict:
    # The PDU's SCTS is tz-corrected to a true UTC epoch in the codec; fall back
    # to "now" only when the stamp is missing/invalid (e.g. a zeroed OTA PDU).
    # The dedup key must NOT include that fallback time — each 15s re-read of
    # the same stored PDU would mint a fresh identity and flap the inbox — so
    # timestamp-less messages key on their raw PDU content instead.
    if decoded.timestamp_epoch is not None:
        ts = decoded.timestamp_epoch
        dedup_src = f"{decoded.sender}|{int(ts)}|{body}"
    else:
        ts = time.time()
        dedup_src = f"{decoded.sender}|pdu|{pdus[0]}"
    # Stable identity so read-state survives the modem's index reuse / refresh.
    dedup = hashlib.sha1(dedup_src.encode(errors="replace")).hexdigest()
    return {
        "ts": ts,
        "peer": decoded.sender,
        "body": body,
        "encoding": decoded.encoding,
        "status": _STATUS.get(status, "read"),
        "modem_indices": indices,
        "parts": parts,
        "raw_pdu": "\n".join(pdus),
        "dedup": dedup,
        "pid": decoded.pid,
        "dcs": decoded.dcs,
        "ota": classify_ota(decoded.pid, decoded.dcs, udh_ieis),
    }
