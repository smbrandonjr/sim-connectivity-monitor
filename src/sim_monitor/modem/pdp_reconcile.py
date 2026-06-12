"""Diff the modem's actual PDP contexts against a profile's desired set.

The modem must end up with EXACTLY the profile's contexts: firmware-auto-created
extras are deleted, mismatched definitions overwritten, missing ones defined.
Pure logic — the daemon executes the returned actions through the driver.
"""

from __future__ import annotations

from dataclasses import dataclass

from sim_monitor.config.schema import PdpContext
from sim_monitor.modem.at_parser import ActualPdpContext


@dataclass(frozen=True)
class DefineContext:
    """Define or overwrite a context (AT+CGDCONT=<cid>,... redefines in place)."""

    context: PdpContext


@dataclass(frozen=True)
class DeleteContext:
    cid: int


Action = DefineContext | DeleteContext


def _matches(actual: ActualPdpContext, desired: PdpContext) -> bool:
    # APNs are case-insensitive (3GPP TS 23.003).
    return actual.pdp_type == desired.pdp_type and actual.apn.lower() == desired.apn.lower()


def reconcile(actual: list[ActualPdpContext], desired: list[PdpContext]) -> list[Action]:
    """Return the actions that make `actual` equal `desired`, no more, no less.

    Deletes come first so a freed cid can never collide with a redefinition.
    """
    desired_by_cid = {c.cid: c for c in desired}
    actual_by_cid = {c.cid: c for c in actual}

    actions: list[Action] = [
        DeleteContext(cid) for cid in sorted(actual_by_cid) if cid not in desired_by_cid
    ]
    for cid in sorted(desired_by_cid):
        existing = actual_by_cid.get(cid)
        if existing is None or not _matches(existing, desired_by_cid[cid]):
            actions.append(DefineContext(desired_by_cid[cid]))
    return actions
