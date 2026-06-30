import selectors
import socket

import pytest

from sim_monitor.config.schema import UdpListenerConfig, UdpReplyRule
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.monitor.udp_listener import UdpListener, effective_udp_config
from sim_monitor.storage.db import Database


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def listener_env(db):
    store = StateStore()
    events = EventLog(db)
    holder: dict = {"cfg": UdpListenerConfig()}
    listener = UdpListener(
        store=store, db=db, events=events, get_config=lambda: holder["cfg"]
    )
    selector = selectors.DefaultSelector()
    yield listener, selector, db, holder
    listener._close_all(selector)


def _deliver(listener, selector, cfg, payload: bytes, port: int) -> socket.socket:
    """Bind, send `payload` to the listener, run one handle iteration; return the
    client socket (so the caller can recv any reply)."""
    listener._sync(selector, cfg)
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.sendto(payload, ("127.0.0.1", port))
    client.settimeout(2.0)
    for key, _ in selector.select(timeout=2.0):
        listener._handle(key.fileobj, key.data, cfg)
    return client


class TestCaptureAndReply:
    def test_captures_and_auto_replies(self, listener_env):
        listener, selector, db, _ = listener_env
        port = free_port()
        cfg = UdpListenerConfig(
            enabled=True, ports=[port],
            rules=[UdpReplyRule(match="contains", pattern="ping", reply="pong")],
        )
        client = _deliver(listener, selector, cfg, b"ping", port)

        data, _ = client.recvfrom(1024)
        assert data == b"pong"
        client.close()

        rows = db.recent_udp_messages()
        dirs = {r["direction"] for r in rows}
        assert dirs == {"in", "out"}
        inbound = next(r for r in rows if r["direction"] == "in")
        assert inbound["body"] == "ping"
        assert inbound["port"] == port
        assert inbound["matched_rule"]  # a rule fired

    def test_no_rule_match_captures_without_reply(self, listener_env):
        listener, selector, db, _ = listener_env
        port = free_port()
        cfg = UdpListenerConfig(
            enabled=True, ports=[port],
            rules=[UdpReplyRule(match="exact", pattern="ping", reply="pong")],
        )
        client = _deliver(listener, selector, cfg, b"hello", port)
        with pytest.raises(socket.timeout):
            client.recvfrom(1024)
        client.close()

        rows = db.recent_udp_messages()
        assert [r["direction"] for r in rows] == ["in"]
        assert rows[0]["matched_rule"] is None

    def test_binary_payload_captured_as_hex_never_replied(self, listener_env):
        listener, selector, db, _ = listener_env
        port = free_port()
        cfg = UdpListenerConfig(
            enabled=True, ports=[port],
            rules=[UdpReplyRule(match="contains", pattern="x", reply="r")],
        )
        client = _deliver(listener, selector, cfg, b"\xff\xfe\x00", port)
        with pytest.raises(socket.timeout):
            client.recvfrom(1024)
        client.close()

        row = db.recent_udp_messages()[0]
        assert row["body"] is None
        assert row["body_hex"] == "fffe00"
        assert row["matched_rule"] is None

    def test_disabled_listener_binds_nothing(self, listener_env):
        listener, selector, _, _ = listener_env
        cfg = UdpListenerConfig(enabled=False, ports=[free_port()])
        listener._sync(selector, cfg)
        assert listener._sockets == {}


class TestRateCap:
    def test_per_peer_rate_cap(self, listener_env):
        listener, _, _, _ = listener_env
        peer = "10.0.0.1:5000"
        allowed = [listener._reply_allowed(peer) for _ in range(listener.REPLY_MAX_PER_PEER + 2)]
        assert allowed[: listener.REPLY_MAX_PER_PEER] == [True] * listener.REPLY_MAX_PER_PEER
        assert allowed[listener.REPLY_MAX_PER_PEER:] == [False, False]

    def test_separate_peers_independent(self, listener_env):
        listener, _, _, _ = listener_env
        assert listener._reply_allowed("a:1")
        assert listener._reply_allowed("b:1")


class TestEffectiveConfig:
    def test_missing_setting_returns_disabled_default(self, db):
        cfg = effective_udp_config(db)
        assert cfg.enabled is False
        assert cfg.ports == []

    def test_invalid_stored_config_falls_back(self, db):
        db.set_setting("udp_listener", {"ports": [99999999]})  # out of range
        cfg = effective_udp_config(db)
        assert cfg.enabled is False

    def test_valid_stored_config_loaded(self, db):
        db.set_setting("udp_listener", {"enabled": True, "ports": [9999]})
        cfg = effective_udp_config(db)
        assert cfg.enabled is True
        assert cfg.ports == [9999]


class TestDb:
    def test_clear_udp_messages(self, db):
        db.add_udp_message(direction="in", port=1, peer="x:1", length=3, body="abc")
        assert len(db.recent_udp_messages()) == 1
        db.clear_udp_messages()
        assert db.recent_udp_messages() == []
