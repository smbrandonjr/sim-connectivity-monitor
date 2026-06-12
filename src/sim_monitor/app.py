"""Composition root: builds and wires the daemon (and, in later phases, the
monitor and web threads) for either simulate or hardware mode."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from sim_monitor.config.schema import AppConfig, Profile
from sim_monitor.core.commands import CommandQueue
from sim_monitor.core.daemon import Daemon
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.storage.db import Database
from sim_monitor.system.sdnotify import SdNotifier

log = logging.getLogger(__name__)


@dataclass
class App:
    config: AppConfig
    store: StateStore
    commands: CommandQueue
    events: EventLog
    db: Database
    daemon: Daemon
    stop: threading.Event


def build(config: AppConfig, profiles: list[Profile]) -> App:
    store = StateStore()
    commands = CommandQueue()
    db = Database(config.db_path)
    events = EventLog(db)

    if config.simulate:
        from sim_monitor.modem.fake import FakeDetector, FakeModemDriver
        from sim_monitor.system.fake_backend import FakeBackend

        driver = FakeModemDriver()
        detector = FakeDetector(driver, appear_after=2)  # modem "enumerates" after ~2 ticks
        backend = FakeBackend(driver)
    else:
        raise NotImplementedError("hardware mode arrives in Phase 3 -- use --simulate")

    daemon = Daemon(
        config=config,
        profiles=profiles,
        detector=detector,
        backend=backend,
        store=store,
        command_queue=commands,
        events=events,
        clock=time.monotonic,
        notifier=SdNotifier(),
    )
    return App(config, store, commands, events, db, daemon, threading.Event())


def run(config: AppConfig, profiles: list[Profile]) -> int:
    app = build(config, profiles)
    daemon_thread = threading.Thread(
        target=app.daemon.run, args=(app.stop,), name="daemon", daemon=True
    )
    daemon_thread.start()
    log.info("daemon thread started; press Ctrl+C to stop")
    try:
        # Phase 2 replaces this idle wait with the waitress/Flask server.
        while daemon_thread.is_alive():
            daemon_thread.join(timeout=1)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        app.stop.set()
        daemon_thread.join(timeout=10)
        app.db.close()
    return 0
