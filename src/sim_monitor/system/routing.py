"""Default-route verification via `ip -j route`."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sim_monitor.system import proc
from sim_monitor.system.backend import BackendError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DefaultRoute:
    interface: str
    metric: int


def parse_default_routes(output: str) -> list[DefaultRoute]:
    """Parse `ip -j route show default` JSON output."""
    try:
        routes = json.loads(output or "[]")
    except json.JSONDecodeError as e:
        raise BackendError(f"unparseable ip route output: {e}") from e
    parsed = []
    for r in routes:
        if "dev" in r:
            parsed.append(DefaultRoute(interface=r["dev"], metric=r.get("metric", 0)))
    return parsed


def preferred_default(routes: list[DefaultRoute]) -> DefaultRoute | None:
    """The default route the kernel will actually use (lowest metric)."""
    return min(routes, key=lambda r: r.metric, default=None)


class Routing:
    def __init__(self, runner: Callable[..., str] = proc.run) -> None:
        self._run = runner

    def default_routes(self) -> list[DefaultRoute]:
        return parse_default_routes(self._run(["ip", "-j", "route", "show", "default"]))

    def interface_is_default(self, interface: str) -> bool:
        best = preferred_default(self.default_routes())
        return best is not None and best.interface == interface
