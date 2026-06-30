"""Pure matching logic for the TCP auto-responder.

Given the listener config and an inbound line's decoded text, decide which rule
(if any) should fire. No I/O — the listener thread owns the socket and writes the
reply. Kept here so it's unit-testable on any platform.
"""

from __future__ import annotations

import re

from sim_monitor.config.schema import TcpListenerConfig, TcpReplyRule


def rule_matches(rule: TcpReplyRule, text: str) -> bool:
    """True if `text` matches this rule. Case-insensitive unless the rule opts
    in. A malformed regex never matches (validation rejects them on save, but be
    defensive against hand-edited DB rows)."""
    if rule.match == "regex":
        flags = 0 if rule.case_sensitive else re.IGNORECASE
        try:
            return re.search(rule.pattern, text, flags) is not None
        except re.error:
            return False

    hay = text if rule.case_sensitive else text.casefold()
    needle = rule.pattern if rule.case_sensitive else rule.pattern.casefold()
    if rule.match == "contains":
        return needle in hay
    if rule.match == "exact":
        return hay.strip() == needle.strip()
    if rule.match == "prefix":
        return hay.lstrip().startswith(needle.strip())
    return False


def find_reply(config: TcpListenerConfig, text: str) -> TcpReplyRule | None:
    """The first enabled rule that matches `text`, or None. Returns None when the
    responder is disabled."""
    if not config.enabled:
        return None
    for rule in config.rules:
        if rule.enabled and rule_matches(rule, text or ""):
            return rule
    return None
