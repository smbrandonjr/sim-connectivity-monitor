import pytest
from pydantic import ValidationError

from sim_monitor.config.schema import TcpListenerConfig, TcpReplyRule
from sim_monitor.core.tcp_reply import find_reply, rule_matches


def rule(**kw):
    base = dict(pattern="x", reply="r")
    base.update(kw)
    return TcpReplyRule(**base)


class TestRuleMatches:
    def test_contains_case_insensitive(self):
        r = rule(match="contains", pattern="status")
        assert rule_matches(r, "Please send STATUS now")
        assert not rule_matches(r, "nothing here")

    def test_case_sensitive(self):
        r = rule(match="contains", pattern="STATUS", case_sensitive=True)
        assert rule_matches(r, "send STATUS")
        assert not rule_matches(r, "send status")

    def test_exact(self):
        r = rule(match="exact", pattern="ping")
        assert rule_matches(r, "ping")
        assert rule_matches(r, "  PING  ")
        assert not rule_matches(r, "ping me")

    def test_prefix(self):
        r = rule(match="prefix", pattern="cmd:")
        assert rule_matches(r, "cmd: reboot")
        assert not rule_matches(r, "do cmd: reboot")

    def test_regex(self):
        r = rule(match="regex", pattern=r"^reboot\b")
        assert rule_matches(r, "reboot now")
        assert not rule_matches(r, "please reboot")

    def test_bad_regex_never_matches(self):
        r = TcpReplyRule.model_construct(
            match="regex", pattern="[unterminated", case_sensitive=False, reply="r",
        )
        assert not rule_matches(r, "anything")


class TestFindReply:
    def test_first_enabled_match_wins(self):
        cfg = TcpListenerConfig(
            enabled=True,
            rules=[
                rule(name="a", pattern="hello", reply="first"),
                rule(name="b", pattern="hello", reply="second"),
            ],
        )
        assert find_reply(cfg, "hello there").reply == "first"

    def test_disabled_rule_skipped(self):
        cfg = TcpListenerConfig(
            enabled=True,
            rules=[
                rule(pattern="hi", reply="skip", enabled=False),
                rule(pattern="hi", reply="use", enabled=True),
            ],
        )
        assert find_reply(cfg, "hi").reply == "use"

    def test_disabled_config_returns_none(self):
        cfg = TcpListenerConfig(enabled=False, rules=[rule(pattern="hi", reply="r")])
        assert find_reply(cfg, "hi") is None

    def test_no_match_returns_none(self):
        cfg = TcpListenerConfig(enabled=True, rules=[rule(pattern="hi", reply="r")])
        assert find_reply(cfg, "bye") is None


class TestSchemaValidation:
    def test_invalid_regex_rejected(self):
        with pytest.raises(ValidationError):
            TcpReplyRule(match="regex", pattern="[unterminated", reply="r")

    def test_blank_pattern_or_reply_rejected(self):
        with pytest.raises(ValidationError):
            TcpReplyRule(pattern="", reply="r")
        with pytest.raises(ValidationError):
            TcpReplyRule(pattern="x", reply="")

    def test_ports_validated_and_deduped(self):
        cfg = TcpListenerConfig(ports=[9998, 9998, 5000])
        assert cfg.ports == [9998, 5000]

    def test_port_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            TcpListenerConfig(ports=[0])
        with pytest.raises(ValidationError):
            TcpListenerConfig(ports=[70000])
