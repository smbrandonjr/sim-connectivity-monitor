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
        from sim_monitor.modem.detect import RealDetector
        from sim_monitor.system.mmcli import Mmcli
        from sim_monitor.system.nmcli import Nmcli
        from sim_monitor.system.real_backend import RealBackend
        from sim_monitor.system.routing import Routing

        mmcli = Mmcli()
        detector = RealDetector(
            mmcli, at_port=config.modem.at_port, baud=config.modem.baud
        )
        backend = RealBackend(
            mmcli, Nmcli(), Routing(), at_port_provider=lambda: detector.last_at_port
        )

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
    from sim_monitor.monitor.http_monitor import HttpMonitor
    from sim_monitor.web import server

    app = build(config, profiles)
    daemon_thread = threading.Thread(
        target=app.daemon.run, args=(app.stop,), name="daemon", daemon=True
    )
    daemon_thread.start()

    monitor = HttpMonitor(
        store=app.store,
        db=app.db,
        events=app.events,
        get_profile=lambda: app.daemon.active_profile,
        trigger=app.daemon.monitor_trigger,
    )
    monitor_thread = threading.Thread(
        target=monitor.run, args=(app.stop,), name="monitor", daemon=True
    )
    monitor_thread.start()

    flask_app = server.create_app(app)
    try:
        server.serve(flask_app, config.web.host, config.web.port)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        app.stop.set()
        daemon_thread.join(timeout=10)
        monitor_thread.join(timeout=5)
        app.db.close()
    return 0
