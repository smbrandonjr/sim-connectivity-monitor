"""Match an inserted SIM's ICCID to the best configuration profile.

Ranking: exact match > longest matching prefix > lowest priority number > name.
Patterns are exact ICCIDs or digit prefixes ending in '*' ('*' alone matches all).
"""

from __future__ import annotations

from dataclasses import dataclass

from sim_monitor.config.schema import Profile


def normalize_iccid(raw: str) -> str:
    """Normalize an ICCID as read from the SIM: strip separators and padding.

    Some modems report a trailing 'F' filler nibble; strip it so user patterns
    written from the printed card number still match.
    """
    s = "".join(ch for ch in raw.strip().upper() if ch.isalnum())
    if s.endswith("F"):
        s = s[:-1]
    return s


@dataclass(frozen=True)
class MatchResult:
    profile: Profile
    pattern: str
    exact: bool


def _pattern_match(iccid: str, pattern: str) -> tuple[bool, int] | None:
    """Return (exact, prefix_len) if the pattern matches, else None."""
    if pattern.endswith("*"):
        prefix = pattern[:-1]
        if iccid.startswith(prefix):
            return (False, len(prefix))
        return None
    if iccid == pattern:
        return (True, len(pattern))
    return None


def match_profile(iccid: str, profiles: list[Profile]) -> MatchResult | None:
    """Pick the best-matching profile for an ICCID, or None if nothing matches."""
    iccid = normalize_iccid(iccid)
    candidates: list[tuple[tuple, MatchResult]] = []
    for profile in profiles:
        for pattern in profile.match.iccid_patterns:
            m = _pattern_match(iccid, pattern)
            if m is None:
                continue
            exact, prefix_len = m
            rank = (0 if exact else 1, -prefix_len, profile.match.priority, profile.name)
            candidates.append((rank, MatchResult(profile, pattern, exact)))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    return candidates[0][1]
