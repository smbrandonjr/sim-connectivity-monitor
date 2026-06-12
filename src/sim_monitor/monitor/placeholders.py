"""Placeholder substitution for monitor request templates.

Templates contain tokens like {iccid} or {signal_rssi}. We deliberately do NOT
use str.format: JSON bodies are full of literal braces and must pass through
untouched. Only {lowercase_identifier} sequences whose name exists in the
context are replaced; everything else (including unknown tokens) is left as-is.
"""

from __future__ import annotations

import re
from typing import Any

TOKEN_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def render(template: str, context: dict[str, Any]) -> tuple[str, set[str]]:
    """Substitute known tokens; return (result, unknown_token_names)."""
    unknown: set[str] = set()

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        if name in context:
            value = context[name]
            return "" if value is None else str(value)
        unknown.add(name)
        return m.group(0)

    return TOKEN_RE.sub(_sub, template), unknown


def render_request(
    url: str, headers: dict[str, str], body: str, context: dict[str, Any]
) -> tuple[str, dict[str, str], str, set[str]]:
    """Render all templated parts of a monitor request."""
    unknown: set[str] = set()
    rendered_url, u = render(url, context)
    unknown |= u
    rendered_headers = {}
    for key, value in headers.items():
        rendered_value, u = render(value, context)
        unknown |= u
        rendered_headers[key] = rendered_value
    rendered_body, u = render(body, context)
    unknown |= u
    return rendered_url, rendered_headers, rendered_body, unknown
