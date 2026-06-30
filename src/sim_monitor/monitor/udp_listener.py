"""UDP listener / auto-responder.

Runs in its own thread and is the sole owner of the listening sockets (the UDP
analog of the daemon owning the serial port). It binds one or more configured
ports, captures every inbound datagram into the DB, and optionally auto-replies
to the sender using pattern->reply rules (first match wins), gated by a per-peer
rate cap so two responders can't ping-pong.

Config is DB-only (settings key 'udp_listener'), read fresh each loop so UI edits
hot-reload without restarting the thread. The pure rule-matching decision lives
in sim_monitor.core.udp_reply.find_reply().
"""

from __future__ import annotations

import logging
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable

from sim_monitor.config.schema import UdpListenerConfig
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.udp_reply import find_reply
from sim_monitor.monitor.transport import SO_BINDTODEVICE
from sim_monitor.storage.db import Database

log = logging.getLogger(__name__)

_MAX_DATAGRAM = 65535


def effective_udp_config(db: Database) -> UdpListenerConfig:
    """The UDP listener config in effect now: the UI-managed setting stored in
    the DB if present (and valid), else an empty (disabled) default. Read fresh
    each loop so UI edits hot-reload."""
    raw = db.get_setting("udp_listener")
    if not raw:
        return UdpListenerConfig()
    try:
        return UdpListenerConfig.model_validate(raw)
    except Exception as e:  # noqa: BLE001 - bad stored config must not wedge the thread
        log.warning("invalid stored udp_listener config (%s); listener disabled", e)
        return UdpListenerConfig()


class UdpListener:
    # Loop guard: cap how many auto-replies a single peer can trigger in a
    # rolling window, so two auto-responders can't ping-pong forever.
    REPLY_WINDOW_SECONDS = 3600
    REPLY_MAX_PER_PEER = 5

    def __init__(
        self,
        store: StateStore,
        db: Database,
        events: EventLog,
        get_config: Callable[[], UdpListenerConfig],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.db = db
        self.events = events
        self.get_config = get_config
        self._monotonic = monotonic
        self._sockets: dict[int, socket.socket] = {}
        # (enabled, ports, bind_interface) of the currently-bound sockets.
        self._bound_key: tuple | None = None
        self._reply_times: dict[str, list[float]] = {}

    def run(self, stop: threading.Event) -> None:
        selector = selectors.DefaultSelector()
        try:
            while not stop.is_set():
                try:
                    config = self.get_config()
                    self._sync(selector, config)
                    if not self._sockets:
                        stop.wait(1.0)  # nothing bound -> idle until config changes
                        continue
                    for key, _ in selector.select(timeout=1.0):
                        self._handle(key.fileobj, key.data, config)
                except Exception:  # noqa: BLE001 - never let the listener thread die
                    log.exception("udp listener iteration failed")
                    stop.wait(1.0)  # avoid a hot spin on a persistent error
        finally:
            self._close_all(selector)

    # ── socket lifecycle ─────────────────────────────────────────────────
    def _sync(self, selector: selectors.BaseSelector, config: UdpListenerConfig) -> None:
        """(Re)bind sockets to match config. No-op when nothing relevant changed."""
        key = (config.enabled, tuple(config.ports), config.bind_interface)
        if key == self._bound_key:
            return
        self._close_all(selector)
        bound: list[int] = []
        errors: list[str] = []
        if config.enabled:
            for port in config.ports:
                try:
                    sock = self._open_socket(port, config.bind_interface)
                except OSError as e:
                    errors.append(f"{port}: {e}")
                    self.events.warning("udp", f"could not bind UDP port {port}: {e}")
                    continue
                self._sockets[port] = sock
                selector.register(sock, selectors.EVENT_READ, port)
                bound.append(port)
            if bound:
                where = config.bind_interface or "all interfaces"
                self.events.info("udp", f"listening on UDP port(s) {bound} ({where})")
        self.db.set_setting(
            "udp_status",
            {"enabled": config.enabled, "ports": bound, "errors": errors},
        )
        self._bound_key = key

    def _open_socket(self, port: int, interface: str) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if interface and sys.platform.startswith("linux"):
            sock.setsockopt(
                socket.SOL_SOCKET, SO_BINDTODEVICE, interface.encode() + b"\0"
            )
        sock.bind(("0.0.0.0", port))
        sock.setblocking(False)
        return sock

    def _close_all(self, selector: selectors.BaseSelector) -> None:
        for sock in self._sockets.values():
            try:
                selector.unregister(sock)
            except (KeyError, ValueError):
                pass
            sock.close()
        self._sockets.clear()

    # ── datagram handling ────────────────────────────────────────────────
    def _handle(
        self, sock: socket.socket, port: int, config: UdpListenerConfig
    ) -> None:
        try:
            data, addr = sock.recvfrom(_MAX_DATAGRAM)
        except (BlockingIOError, OSError):
            return
        peer = f"{addr[0]}:{addr[1]}"
        try:
            text: str | None = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None  # binary payload: captured as hex, never auto-replied
        rule = find_reply(config, text) if text is not None else None
        label = (rule.name or rule.pattern) if rule else None
        self.db.add_udp_message(
            direction="in", port=port, peer=peer, length=len(data),
            body=text, body_hex=data.hex(), matched_rule=label,
        )
        if rule is None:
            return
        if not self._reply_allowed(peer):
            self.events.warning(
                "udp",
                f"auto-reply to {peer} suppressed (over "
                f"{self.REPLY_MAX_PER_PEER}/hr loop guard)",
            )
            return
        payload = rule.reply.encode("utf-8")
        try:
            sock.sendto(payload, addr)
        except OSError as e:
            self.events.error("udp", f"reply to {peer} failed: {e}")
            return
        self.events.info("udp", f"auto-reply rule {label!r} matched datagram from {peer}")
        self.db.add_udp_message(
            direction="out", port=port, peer=peer, length=len(payload),
            body=rule.reply, body_hex=payload.hex(), matched_rule=label,
        )

    def _reply_allowed(self, peer: str) -> bool:
        """Rate-limit auto-replies per peer (rolling window). Records the send
        when allowed."""
        now = self._monotonic()
        cutoff = now - self.REPLY_WINDOW_SECONDS
        recent = [t for t in self._reply_times.get(peer, []) if t >= cutoff]
        if len(recent) >= self.REPLY_MAX_PER_PEER:
            self._reply_times[peer] = recent
            return False
        recent.append(now)
        self._reply_times[peer] = recent
        return True
