"""HTTP transport for the monitor.

Phase 4 adds an adapter that binds the socket to the cellular interface
(SO_BINDTODEVICE) so a successful probe genuinely proves cellular egress.
Until then (and always in simulate mode) this is a plain requests session.
"""

from __future__ import annotations

import requests


def make_session(interface: str | None = None) -> requests.Session:
    # TODO(Phase 4): bind to `interface` via SO_BINDTODEVICE on Linux.
    return requests.Session()
