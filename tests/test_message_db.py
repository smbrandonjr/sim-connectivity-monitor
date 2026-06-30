"""Paging, per-row delete, and unread-watermark DB behavior for the message
capture logs (SMS/UDP/TCP)."""

import pytest

from sim_monitor.storage.db import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


class TestPaging:
    def test_udp_paging_and_count(self, db):
        for i in range(5):
            db.add_udp_message(direction="in", port=1, peer="p:1", length=1, body=str(i))
        assert db.count_udp_messages() == 5
        first = db.recent_udp_messages(limit=2, offset=0)
        assert [r["body"] for r in first] == ["4", "3"]  # newest first
        second = db.recent_udp_messages(limit=2, offset=2)
        assert [r["body"] for r in second] == ["2", "1"]

    def test_tcp_paging_and_count(self, db):
        for i in range(3):
            db.add_tcp_message(direction="in", port=1, peer="p:1", length=1, body=str(i))
        assert db.count_tcp_messages() == 3
        assert len(db.recent_tcp_messages(limit=2, offset=2)) == 1

    def test_sms_count_and_offset(self, db):
        for i in range(3):
            db.add_sent_sms("123", f"m{i}")
        assert db.count_sms() == 3
        assert len(db.recent_sms(limit=2, offset=2)) == 1


class TestDelete:
    def test_delete_udp_row(self, db):
        db.add_udp_message(direction="in", port=1, peer="p:1", length=1, body="a")
        rid = db.recent_udp_messages()[0]["id"]
        db.delete_udp_message(rid)
        assert db.count_udp_messages() == 0

    def test_delete_tcp_row(self, db):
        db.add_tcp_message(direction="in", port=1, peer="p:1", length=1, body="a")
        rid = db.recent_tcp_messages()[0]["id"]
        db.delete_tcp_message(rid)
        assert db.count_tcp_messages() == 0

    def test_clear_tcp(self, db):
        db.add_tcp_message(direction="in", port=1, peer="p:1", length=1, body="a")
        db.clear_tcp_messages()
        assert db.recent_tcp_messages() == []


class TestUnreadWatermark:
    def test_udp_unread_counts_only_inbound(self, db):
        for i in range(3):
            db.add_udp_message(direction="in", port=1, peer="p:1", length=1, body=str(i))
        db.add_udp_message(direction="out", port=1, peer="p:1", length=1, body="reply")
        assert db.count_unread_udp() == 3
        db.mark_udp_read()
        assert db.count_unread_udp() == 0
        db.add_udp_message(direction="in", port=1, peer="p:1", length=1, body="new")
        assert db.count_unread_udp() == 1

    def test_tcp_unread_watermark(self, db):
        db.add_tcp_message(direction="in", port=1, peer="p:1", length=1, body="a")
        assert db.count_unread_tcp() == 1
        db.mark_tcp_read()
        assert db.count_unread_tcp() == 0

    def test_mark_read_empty_is_safe(self, db):
        db.mark_udp_read()
        db.mark_tcp_read()
        assert db.count_unread_udp() == 0
        assert db.count_unread_tcp() == 0
