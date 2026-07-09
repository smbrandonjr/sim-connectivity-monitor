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
    scan: object  # scan.manager.ScanManager (network scanner, off the modem path)
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
        db=db,
        clock=time.monotonic,
        notifier=SdNotifier(),
    )
    from sim_monitor.scan.manager import ScanManager

    scan = ScanManager(simulate=config.simulate)
    return App(config, store, commands, events, db, daemon, scan, threading.Event())


def run(config: AppConfig, profiles: list[Profile]) -> int:
    from sim_monitor.monitor.http_check_monitor import (
        HttpCheckMonitor,
        effective_http_check_config,
        make_fake_http_prober,
    )
    from sim_monitor.monitor.http_monitor import HttpMonitor
    from sim_monitor.monitor.ping_monitor import (
        SIMULATE_INTERFACES,
        PingMonitor,
        effective_latency_config,
        make_fake_pinger,
    )
    from sim_monitor.monitor.tcp_listener import TcpListener, effective_tcp_config
    from sim_monitor.monitor.udp_listener import UdpListener, effective_udp_config
    from sim_monitor.traffic.collector import TrafficCollector, effective_traffic_config
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
        get_config=app.daemon.effective_monitor_config,
        trigger=app.daemon.monitor_trigger,
    )
    monitor_thread = threading.Thread(
        target=monitor.run, args=(app.stop,), name="monitor", daemon=True
    )
    monitor_thread.start()

    # Per-interface latency/packet-loss (ICMP) monitor. Real `ping` doesn't exist
    # on the dev box, so simulate mode injects a fake pinger over a fixed
    # interface set.
    ping_kwargs: dict = {}
    http_check_kwargs: dict = {}
    if config.simulate:
        ping_kwargs = {
            "pinger": make_fake_pinger(),
            "list_interfaces": lambda: list(SIMULATE_INTERFACES),
        }
        http_check_kwargs = {
            "prober": make_fake_http_prober(),
            "list_interfaces": lambda: list(SIMULATE_INTERFACES),
        }
    ping_monitor = PingMonitor(
        store=app.store,
        db=app.db,
        events=app.events,
        get_config=lambda: effective_latency_config(app.db, app.config.latency),
        **ping_kwargs,
    )
    ping_thread = threading.Thread(
        target=ping_monitor.run, args=(app.stop,), name="ping-monitor", daemon=True
    )
    ping_thread.start()

    # Per-interface HTTP/website reachability monitor (separate storage + config).
    http_check_monitor = HttpCheckMonitor(
        store=app.store,
        db=app.db,
        events=app.events,
        get_config=lambda: effective_http_check_config(app.db, app.config.http_checks),
        **http_check_kwargs,
    )
    http_check_thread = threading.Thread(
        target=http_check_monitor.run, args=(app.stop,), name="http-check-monitor",
        daemon=True,
    )
    http_check_thread.start()

    # UDP listener/responder: binds configured ports, captures datagrams, and
    # optionally auto-replies. Owns its sockets (the UDP analog of the daemon's
    # serial ownership); config is DB-only and read fresh each loop.
    udp_listener = UdpListener(
        store=app.store,
        db=app.db,
        events=app.events,
        get_config=lambda: effective_udp_config(app.db),
    )
    udp_thread = threading.Thread(
        target=udp_listener.run, args=(app.stop,), name="udp-listener", daemon=True
    )
    udp_thread.start()

    # TCP listener/responder: same as UDP but connection-oriented (line-framed).
    tcp_listener = TcpListener(
        store=app.store,
        db=app.db,
        events=app.events,
        get_config=lambda: effective_tcp_config(app.db),
    )
    tcp_thread = threading.Thread(
        target=tcp_listener.run, args=(app.stop,), name="tcp-listener", daemon=True
    )
    tcp_thread.start()

    # Traffic auditor: records every conntrack flow (any interface, any
    # direction) so "did this device talk to IP X on port Y, how much" is
    # always answerable from the DB.
    if config.simulate:
        from sim_monitor.traffic.sources import FakeFlowSource

        traffic_source = FakeFlowSource()
        traffic_ip_map = traffic_source.ip_map
        traffic_backend = "simulate"
    else:
        from sim_monitor.system.netifaces import list_ip_interface_map
        from sim_monitor.traffic.sources import ConntrackSource

        traffic_source = ConntrackSource()
        traffic_ip_map = list_ip_interface_map
        traffic_backend = "conntrack"
    traffic = TrafficCollector(
        db=app.db,
        events=app.events,
        get_config=lambda: effective_traffic_config(app.db, app.config.traffic),
        source=traffic_source,
        ip_interfaces=traffic_ip_map,
        backend_name=traffic_backend,
    )
    traffic_thread = threading.Thread(
        target=traffic.run, args=(app.stop,), name="traffic", daemon=True
    )
    traffic_thread.start()

    flask_app = server.create_app(app)
    try:
        server.serve(flask_app, config.web.host, config.web.port)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        app.stop.set()
        daemon_thread.join(timeout=10)
        monitor_thread.join(timeout=5)
        ping_thread.join(timeout=5)
        http_check_thread.join(timeout=5)
        udp_thread.join(timeout=5)
        tcp_thread.join(timeout=5)
        traffic_thread.join(timeout=5)
        app.db.close()
    return 0
