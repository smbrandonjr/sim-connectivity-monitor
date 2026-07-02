"""TCP listener / auto-responder.

Runs in its own thread and is the sole owner of the listening sockets (the TCP
analog of the daemon owning the serial port). It binds one or more configured
ports, accepts connections, and captures every inbound line (newline-delimited)
into the DB, optionally auto-replying on the same connection using pattern->reply
rules (first match wins), gated by a per-peer rate cap so two responders can't
ping-pong. Connections stay open for more lines until the peer closes.

TCP is a byte stream, so a per-connection buffer accumulates bytes and splits on
"\n"; a complete line is one captured message. The buffer is capped so a peer
that never sends a newline can't grow it unbounded.

Config is DB-only (settings key 'tcp_listener'), read fresh each loop so UI edits
hot-reload without restarting the thread. The pure rule-matching decision lives
in sim_monitor.core.tcp_reply.find_reply().
"""

from __future__ import annotations

import logging
import selectors
import socket
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sim_monitor.config.schema import TcpListenerConfig
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.tcp_reply import find_reply
from sim_monitor.monitor.http_monitor import resolve_egress
from sim_monitor.monitor.transport import SO_BINDTODEVICE
from sim_monitor.storage.db import Database
from sim_monitor.system.netifaces import list_up_interfaces

log = logging.getLogger(__name__)

_RECV_SIZE = 4096
_MAX_LINE_BYTES = 65536  # flush an over-long un-terminated buffer as one message
_MAX_CONNECTIONS = 64    # cap concurrent accepted connections


def effective_tcp_config(db: Database) -> TcpListenerConfig:
    """The TCP listener config in effect now: the UI-managed setting stored in
    the DB if present (and valid), else an empty (disabled) default. Read fresh
    each loop so UI edits hot-reload."""
    raw = db.get_setting("tcp_listener")
    if not raw:
        return TcpListenerConfig()
    try:
        return TcpListenerConfig.model_validate(raw)
    except Exception as e:  # noqa: BLE001 - bad stored config must not wedge the thread
        log.warning("invalid stored tcp_listener config (%s); listener disabled", e)
        return TcpListenerConfig()


@dataclass
class _Conn:
    """State for one accepted client connection."""

    sock: socket.socket
    peer: str
    port: int
    buf: bytearray = field(default_factory=bytearray)


class TcpListener:
    # Loop guard: cap how many auto-replies a single peer can trigger in a
    # rolling window, so two auto-responders can't ping-pong forever.
    REPLY_WINDOW_SECONDS = 3600
    REPLY_MAX_PER_PEER = 5

    def __init__(
        self,
        store: StateStore,
        db: Database,
        events: EventLog,
        get_config: Callable[[], TcpListenerConfig],
        monotonic: Callable[[], float] = time.monotonic,
        list_interfaces: Callable[[], list[str]] = list_up_interfaces,
    ) -> None:
        self.store = store
        self.db = db
        self.events = events
        self.get_config = get_config
        self._monotonic = monotonic
        self.list_interfaces = list_interfaces
        # Listening sockets keyed by port; accepted client connections keyed by fd.
        self._listeners: dict[int, socket.socket] = {}
        self._conns: dict[int, _Conn] = {}
        # (enabled, ports, egress, interface) of the currently-bound listeners.
        self._bound_key: tuple | None = None
        self._reply_times: dict[str, list[float]] = {}

    def run(self, stop: threading.Event) -> None:
        selector = selectors.DefaultSelector()
        try:
            while not stop.is_set():
                try:
                    config = self.get_config()
                    self._sync(selector, config)
                    if not self._listeners and not self._conns:
                        stop.wait(1.0)  # nothing bound -> idle until config changes
                        continue
                    for key, _ in selector.select(timeout=1.0):
                        if isinstance(key.data, int):
                            self._accept(selector, key.fileobj, key.data, config)
                        else:
                            self._read(selector, key.data, config)
                except Exception:  # noqa: BLE001 - never let the listener thread die
                    log.exception("tcp listener iteration failed")
                    stop.wait(1.0)  # avoid a hot spin on a persistent error
        finally:
            self._close_all(selector)

    # ── socket lifecycle ─────────────────────────────────────────────────
    def _sync(self, selector: selectors.BaseSelector, config: TcpListenerConfig) -> None:
        """(Re)bind listening sockets to match config. No-op when nothing relevant
        changed. The configured egress (wlan/cellular/auto) is resolved to a live
        netdev each call, so the listeners rebind when the interface comes up."""
        interface = resolve_egress(config, self.store.get(), self.list_interfaces)
        key = (config.enabled, tuple(config.ports), config.egress, interface)
        if key == self._bound_key:
            return
        self._drop_listeners(selector)
        # A rebind also drops in-flight connections (their listener may be gone).
        self._drop_conns(selector)
        bound: list[int] = []
        errors: list[str] = []
        if config.enabled:
            for port in config.ports:
                try:
                    sock = self._open_socket(port, interface)
                except OSError as e:
                    errors.append(f"{port}: {e}")
                    self.events.warning("tcp", f"could not bind TCP port {port}: {e}")
                    continue
                self._listeners[port] = sock
                selector.register(sock, selectors.EVENT_READ, port)
                bound.append(port)
            if bound:
                where = interface or "all interfaces"
                self.events.info("tcp", f"listening on TCP port(s) {bound} ({where})")
        self.db.set_setting(
            "tcp_status",
            {
                "enabled": config.enabled, "egress": config.egress,
                "interface": interface, "ports": bound, "errors": errors,
            },
        )
        self._bound_key = key

    def _open_socket(self, port: int, interface: str | None) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if interface and sys.platform.startswith("linux"):
            sock.setsockopt(
                socket.SOL_SOCKET, SO_BINDTODEVICE, interface.encode() + b"\0"
            )
        sock.bind(("0.0.0.0", port))
        sock.listen(16)
        sock.setblocking(False)
        return sock

    def _accept(
        self,
        selector: selectors.BaseSelector,
        listener: socket.socket,
        port: int,
        config: TcpListenerConfig,
    ) -> None:
        try:
            client, addr = listener.accept()
        except OSError:
            return
        if len(self._conns) >= _MAX_CONNECTIONS:
            client.close()
            self.events.warning("tcp", "connection cap reached; dropping new connection")
            return
        client.setblocking(False)
        conn = _Conn(sock=client, peer=f"{addr[0]}:{addr[1]}", port=port)
        self._conns[client.fileno()] = conn
        selector.register(client, selectors.EVENT_READ, conn)

    def _read(
        self, selector: selectors.BaseSelector, conn: _Conn, config: TcpListenerConfig
    ) -> None:
        try:
            data = conn.sock.recv(_RECV_SIZE)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop_conn(selector, conn)
            return
        if not data:  # peer closed
            # Flush a trailing un-terminated line so a client that sends a
            # payload without a newline and closes (common with simple tools:
            # `printf hi | nc`, a one-shot sendall) is still captured.
            if conn.buf:
                self._handle_line(conn, bytes(conn.buf), config)
                conn.buf = bytearray()
            self._drop_conn(selector, conn)
            return
        conn.buf.extend(data)
        while b"\n" in conn.buf:
            line, _, rest = conn.buf.partition(b"\n")
            conn.buf = bytearray(rest)
            self._handle_line(conn, bytes(line).rstrip(b"\r"), config)
        if len(conn.buf) > _MAX_LINE_BYTES:
            # A peer streaming without newlines: flush what we have as one message.
            line = bytes(conn.buf)
            conn.buf = bytearray()
            self._handle_line(conn, line, config)

    def _handle_line(self, conn: _Conn, raw: bytes, config: TcpListenerConfig) -> None:
        if not raw:
            return  # skip empty lines (e.g. blank keepalive)
        try:
            text: str | None = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = None  # binary payload: captured as hex, never auto-replied
        rule = find_reply(config, text) if text is not None else None
        label = (rule.name or rule.pattern) if rule else None
        self.db.add_tcp_message(
            direction="in", port=conn.port, peer=conn.peer, length=len(raw),
            body=text, body_hex=raw.hex(), matched_rule=label,
        )
        if rule is None:
            return
        if not self._reply_allowed(conn.peer):
            self.events.warning(
                "tcp",
                f"auto-reply to {conn.peer} suppressed (over "
                f"{self.REPLY_MAX_PER_PEER}/hr loop guard)",
            )
            return
        payload = rule.reply.encode("utf-8")
        try:
            conn.sock.sendall(payload)
        except OSError as e:
            self.events.error("tcp", f"reply to {conn.peer} failed: {e}")
            return
        self.events.info(
            "tcp", f"auto-reply rule {label!r} matched line from {conn.peer}"
        )
        self.db.add_tcp_message(
            direction="out", port=conn.port, peer=conn.peer, length=len(payload),
            body=rule.reply, body_hex=payload.hex(), matched_rule=label,
        )

    def _drop_conn(self, selector: selectors.BaseSelector, conn: _Conn) -> None:
        try:
            selector.unregister(conn.sock)
        except (KeyError, ValueError):
            pass
        self._conns.pop(conn.sock.fileno(), None)
        conn.sock.close()

    def _drop_listeners(self, selector: selectors.BaseSelector) -> None:
        for sock in self._listeners.values():
            try:
                selector.unregister(sock)
            except (KeyError, ValueError):
                pass
            sock.close()
        self._listeners.clear()

    def _drop_conns(self, selector: selectors.BaseSelector) -> None:
        for conn in list(self._conns.values()):
            try:
                selector.unregister(conn.sock)
            except (KeyError, ValueError):
                pass
            conn.sock.close()
        self._conns.clear()

    def _close_all(self, selector: selectors.BaseSelector) -> None:
        self._drop_conns(selector)
        self._drop_listeners(selector)

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
