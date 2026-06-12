"""Structured event log: every notable daemon occurrence goes to both the
Python logger (journald on the Pi) and SQLite (for the web UI)."""

from __future__ import annotations

import logging
from typing import Any

from sim_monitor.storage.db import Database

log = logging.getLogger("sim_monitor.events")

_LEVELS = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class EventLog:
    def __init__(self, db: Database) -> None:
        self._db = db

    def emit(
        self, level: str, kind: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        log.log(_LEVELS.get(level, logging.INFO), "[%s] %s", kind, message)
        try:
            self._db.add_event(level, kind, message, data)
        except Exception:
            log.exception("failed to persist event")

    def info(self, kind: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.emit("info", kind, message, data)

    def warning(self, kind: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.emit("warning", kind, message, data)

    def error(self, kind: str, message: str, data: dict[str, Any] | None = None) -> None:
        self.emit("error", kind, message, data)
