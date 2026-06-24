"""Pure matching logic for the SMS auto-responder.

Given the auto-reply config and an inbound message body, decide which rule (if
any) should fire. No I/O — the daemon owns sending the reply. Kept here so it's
unit-testable on any platform.
"""

from __future__ import annotations

import re

from sim_monitor.config.schema import SmsAutoReplyConfig, SmsReplyRule


def rule_matches(rule: SmsReplyRule, body: str) -> bool:
    """True if `body` matches this rule. Case-insensitive unless the rule opts
    in. A malformed regex never matches (validation rejects them on save, but be
    defensive against hand-edited DB rows)."""
    if rule.match == "regex":
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        try:
            return re.search(rule.pattern, body, flags) is not None
        except re.error:
            return False

    hay = body if rule.case_sensitive else body.casefold()
    needle = rule.pattern if rule.case_sensitive else rule.pattern.casefold()
    if rule.match == "contains":
        return needle in hay
    if rule.match == "exact":
        return hay.strip() == needle.strip()
    if rule.match == "prefix":
        return hay.lstrip().startswith(needle.strip())
    return False


def find_reply(config: SmsAutoReplyConfig, body: str) -> SmsReplyRule | None:
    """The first enabled rule that matches `body`, or None. Returns None when
    the responder is disabled."""
    if not config.enabled:
        return None
    for rule in config.rules:
        if rule.enabled and rule_matches(rule, body or ""):
            return rule
    return None
