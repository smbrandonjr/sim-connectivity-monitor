import selectors
import socket
import threading
import time

import pytest

from sim_monitor.config.schema import TcpListenerConfig, TcpReplyRule
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.monitor.tcp_listener import TcpListener, effective_tcp_config
from sim_monitor.storage.db import Database


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def make_listener(db, cfg):
    return TcpListener(
        store=StateStore(), db=db, events=EventLog(db), get_config=lambda: cfg
    )


def connect_retry(port, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        try:
            s.connect(("127.0.0.1", port))
            return s
        except OSError:
            s.close()
            time.sleep(0.05)
    raise AssertionError("could not connect to listener")


def wait_rows(db, n, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and db.count_tcp_messages() < n:
        time.sleep(0.03)


class TestLiveListener:
    """Drive the real run() loop in a thread against a loopback connection."""

    def _run(self, listener):
        stop = threading.Event()
        t = threading.Thread(target=listener.run, args=(stop,), daemon=True)
        t.start()
        return stop, t

    def test_line_capture_and_reply(self, db):
        port = free_port()
        cfg = TcpListenerConfig(
            enabled=True, ports=[port],
            rules=[TcpReplyRule(match="exact", pattern="ping", reply="pong\n")],
        )
        listener = make_listener(db, cfg)
        stop, t = self._run(listener)
        try:
            c = connect_retry(port)
            c.sendall(b"ping\nhello\n")
            c.settimeout(2.0)
            assert c.recv(100) == b"pong\n"
            wait_rows(db, 3)
            c.close()
        finally:
            stop.set()
            t.join(timeout=2)
        rows = db.recent_tcp_messages()
        assert sorted(r["direction"] for r in rows) == ["in", "in", "out"]
        bodies = {(r["direction"], r["body"]) for r in rows}
        assert ("in", "ping") in bodies
        assert ("in", "hello") in bodies
        assert ("out", "pong\n") in bodies

    def test_partial_line_buffered(self, db):
        port = free_port()
        cfg = TcpListenerConfig(
            enabled=True, ports=[port],
            rules=[TcpReplyRule(match="exact", pattern="ping", reply="pong\n")],
        )
        listener = make_listener(db, cfg)
        stop, t = self._run(listener)
        try:
            c = connect_retry(port)
            c.sendall(b"pi")
            time.sleep(0.2)
            c.sendall(b"ng\n")
            c.settimeout(2.0)
            assert c.recv(100) == b"pong\n"
            wait_rows(db, 2)
            c.close()
        finally:
            stop.set()
            t.join(timeout=2)
        ins = [r for r in db.recent_tcp_messages() if r["direction"] == "in"]
        assert len(ins) == 1
        assert ins[0]["body"] == "ping"

    def test_unterminated_line_flushed_on_close(self, db):
        # A client that sends a payload without a trailing newline and then
        # closes must still be captured (common with `printf hi | nc` and one-
        # shot sendall clients). Regression for silently-dropped-on-close.
        port = free_port()
        cfg = TcpListenerConfig(enabled=True, ports=[port])
        listener = make_listener(db, cfg)
        stop, t = self._run(listener)
        try:
            c = connect_retry(port)
            c.sendall(b"no-newline-here")  # no trailing "\n"
            c.close()                       # peer closes -> must flush buffer
            wait_rows(db, 1)
        finally:
            stop.set()
            t.join(timeout=2)
        ins = [r for r in db.recent_tcp_messages() if r["direction"] == "in"]
        assert len(ins) == 1
        assert ins[0]["body"] == "no-newline-here"

    def test_binary_line_hex_no_reply(self, db):
        port = free_port()
        cfg = TcpListenerConfig(
            enabled=True, ports=[port],
            rules=[TcpReplyRule(match="contains", pattern="x", reply="r")],
        )
        listener = make_listener(db, cfg)
        stop, t = self._run(listener)
        try:
            c = connect_retry(port)
            c.sendall(b"\xff\xfe\x00\n")
            c.settimeout(0.6)
            with pytest.raises(socket.timeout):
                c.recv(100)
            wait_rows(db, 1)
            c.close()
        finally:
            stop.set()
            t.join(timeout=2)
        rows = db.recent_tcp_messages()
        assert len(rows) == 1
        assert rows[0]["body"] is None
        assert rows[0]["body_hex"] == "fffe00"
        assert rows[0]["matched_rule"] is None


class TestUnit:
    def test_disabled_binds_nothing(self, db):
        listener = make_listener(db, TcpListenerConfig(enabled=False, ports=[free_port()]))
        sel = selectors.DefaultSelector()
        listener._sync(sel, listener.get_config())
        assert listener._listeners == {}
        listener._close_all(sel)

    def test_reply_rate_cap(self, db):
        listener = make_listener(db, TcpListenerConfig())
        peer = "1.2.3.4:9"
        res = [listener._reply_allowed(peer) for _ in range(listener.REPLY_MAX_PER_PEER + 2)]
        assert res[: listener.REPLY_MAX_PER_PEER] == [True] * listener.REPLY_MAX_PER_PEER
        assert res[listener.REPLY_MAX_PER_PEER:] == [False, False]

    def test_effective_config_fallback(self, db):
        assert effective_tcp_config(db).enabled is False
        db.set_setting("tcp_listener", {"ports": [99999999]})  # out of range
        assert effective_tcp_config(db).enabled is False
        db.set_setting("tcp_listener", {"enabled": True, "ports": [9998]})
        cfg = effective_tcp_config(db)
        assert cfg.enabled is True
        assert cfg.ports == [9998]
