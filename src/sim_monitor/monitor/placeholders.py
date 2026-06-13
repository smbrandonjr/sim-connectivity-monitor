"""Placeholder substitution for monitor request templates.

Templates contain tokens like {iccid} or {signal_rssi}. We deliberately do NOT
use str.format: JSON bodies are full of literal braces and must pass through
untouched. Only {lowercase_identifier} sequences whose name exists in the
context are replaced; everything else (including unknown tokens) is left as-is.
"""

from __future__ import annotations

import json
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


def render_body_fields(fields: list, context: dict[str, Any]) -> str:
    """Assemble a JSON body from structured fields. Placeholder fields resolve
    to their native-typed value (number stays a number, string a string) and
    are OMITTED when unknown/None — so the result is always valid JSON. Static
    fields contribute their literal string. `fields` items have .path/.value/
    .kind (a BodyField or any object/dict with those keys)."""
    out: dict[str, Any] = {}
    for f in fields:
        path = _attr(f, "path")
        value = _attr(f, "value")
        kind = _attr(f, "kind") or "placeholder"
        if kind == "placeholder":
            resolved = context.get(value)
            if resolved is None:
                continue  # unknown -> omit (keeps JSON valid)
        else:
            resolved = value
        _set_path(out, path.split("."), resolved)
    return json.dumps(out)


def _attr(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name)


def _set_path(root: dict, parts: list[str], value: Any) -> None:
    node = root
    for p in parts[:-1]:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):  # a leaf already occupies this path
            return
    node[parts[-1]] = value


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
