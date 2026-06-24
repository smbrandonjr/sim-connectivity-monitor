import pytest
from pydantic import ValidationError

from sim_monitor.config.schema import SmsAutoReplyConfig, SmsReplyRule
from sim_monitor.core.sms_reply import find_reply, rule_matches


def rule(**kw):
    base = dict(pattern="x", reply="r")
    base.update(kw)
    return SmsReplyRule(**base)


class TestRuleMatches:
    def test_contains_case_insensitive(self):
        r = rule(match="contains", pattern="status")
        assert rule_matches(r, "Please send STATUS now")
        assert rule_matches(r, "status")
        assert not rule_matches(r, "nothing here")

    def test_case_sensitive(self):
        r = rule(match="contains", pattern="STATUS", case_sensitive=True)
        assert rule_matches(r, "send STATUS")
        assert not rule_matches(r, "send status")

    def test_exact(self):
        r = rule(match="exact", pattern="ping")
        assert rule_matches(r, "ping")
        assert rule_matches(r, "  PING  ")  # trimmed + case-insensitive
        assert not rule_matches(r, "ping me")

    def test_prefix(self):
        r = rule(match="prefix", pattern="cmd:")
        assert rule_matches(r, "cmd: reboot")
        assert rule_matches(r, "  CMD: reboot")
        assert not rule_matches(r, "do cmd: reboot")

    def test_regex(self):
        r = rule(match="regex", pattern=r"^reboot\b")
        assert rule_matches(r, "reboot now")
        assert rule_matches(r, "REBOOT")  # case-insensitive by default
        assert not rule_matches(r, "please reboot")

    def test_regex_case_sensitive(self):
        r = rule(match="regex", pattern=r"^OK$", case_sensitive=True)
        assert rule_matches(r, "OK")
        assert not rule_matches(r, "ok")


class TestFindReply:
    def test_first_enabled_match_wins(self):
        cfg = SmsAutoReplyConfig(
            enabled=True,
            rules=[
                rule(name="a", pattern="hello", reply="first"),
                rule(name="b", pattern="hello", reply="second"),
            ],
        )
        assert find_reply(cfg, "hello there").reply == "first"

    def test_disabled_rule_skipped(self):
        cfg = SmsAutoReplyConfig(
            enabled=True,
            rules=[
                rule(pattern="hi", reply="skip", enabled=False),
                rule(pattern="hi", reply="use", enabled=True),
            ],
        )
        assert find_reply(cfg, "hi").reply == "use"

    def test_disabled_config_returns_none(self):
        cfg = SmsAutoReplyConfig(enabled=False, rules=[rule(pattern="hi", reply="r")])
        assert find_reply(cfg, "hi") is None

    def test_no_match_returns_none(self):
        cfg = SmsAutoReplyConfig(enabled=True, rules=[rule(pattern="hi", reply="r")])
        assert find_reply(cfg, "bye") is None

    def test_empty_body_safe(self):
        cfg = SmsAutoReplyConfig(enabled=True, rules=[rule(pattern="hi", reply="r")])
        assert find_reply(cfg, "") is None


class TestSchemaValidation:
    def test_invalid_regex_rejected(self):
        with pytest.raises(ValidationError):
            SmsReplyRule(match="regex", pattern="[unterminated", reply="r")

    def test_valid_regex_accepted(self):
        SmsReplyRule(match="regex", pattern=r"\d+", reply="r")

    def test_blank_pattern_or_reply_rejected(self):
        with pytest.raises(ValidationError):
            SmsReplyRule(pattern="", reply="r")
        with pytest.raises(ValidationError):
            SmsReplyRule(pattern="x", reply="")
