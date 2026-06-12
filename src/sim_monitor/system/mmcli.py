"""ModemManager CLI wrapper (JSON output parsing)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from sim_monitor.system import proc
from sim_monitor.system.backend import BackendError

log = logging.getLogger(__name__)


def _modem_index(path: str) -> int:
    """/org/freedesktop/ModemManager1/Modem/3 -> 3"""
    return int(path.rstrip("/").rsplit("/", 1)[-1])


class Mmcli:
    def __init__(self, runner: Callable[..., str] = proc.run) -> None:
        self._run = runner

    def list_modems(self) -> list[int]:
        out = self._run(["mmcli", "-L", "-J"])
        try:
            paths = json.loads(out).get("modem-list", [])
        except json.JSONDecodeError as e:
            raise BackendError(f"unparseable mmcli -L output: {e}") from e
        return sorted(_modem_index(p) for p in paths)

    def first_modem(self) -> int | None:
        modems = self.list_modems()
        return modems[0] if modems else None

    def get_modem(self, index: int) -> dict:
        out = self._run(["mmcli", "-m", str(index), "-J"])
        try:
            return json.loads(out)["modem"]
        except (json.JSONDecodeError, KeyError) as e:
            raise BackendError(f"unparseable mmcli -m output: {e}") from e

    def modem_state(self, index: int) -> str:
        return self.get_modem(index).get("generic", {}).get("state", "unknown")

    def enable(self, index: int) -> None:
        self._run(["mmcli", "-m", str(index), "--enable"], timeout=60)

    def disable(self, index: int) -> None:
        self._run(["mmcli", "-m", str(index), "--disable"], timeout=60)
