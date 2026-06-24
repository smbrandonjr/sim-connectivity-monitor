"""Pydantic models for the app config and configuration profiles.

Everything user-editable (config.yaml, profiles.d/*.yaml) is validated through
these models so YAML mistakes surface as readable errors instead of runtime
failures deep in the daemon.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PdpType = Literal["IPv4", "IPv6", "IPv4v6"]
AuthType = Literal["none", "pap", "chap"]

MAX_PDP_CONTEXTS = 3


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PdpContext(StrictModel):
    cid: int = Field(ge=1, le=16)
    apn: str
    pdp_type: PdpType = "IPv4"
    auth: AuthType = "none"
    username: str = ""
    password: str = ""
    bearer: bool = False

    @model_validator(mode="after")
    def _auth_requires_credentials(self) -> PdpContext:
        if self.auth != "none" and not self.username:
            raise ValueError(f"cid {self.cid}: auth={self.auth} requires a username")
        return self


class MatchSpec(StrictModel):
    """ICCID matching: patterns are exact ICCIDs or prefixes ending in '*'."""

    iccid_patterns: list[str] = Field(default=["*"], min_length=1)
    priority: int = 100

    @field_validator("iccid_patterns")
    @classmethod
    def _validate_patterns(cls, patterns: list[str]) -> list[str]:
        cleaned = []
        for p in patterns:
            p = p.strip()
            if not p:
                raise ValueError("empty ICCID pattern")
            digits = p[:-1] if p.endswith("*") else p
            if digits and not digits.isdigit():
                raise ValueError(f"ICCID pattern {p!r} must be digits, optionally ending in '*'")
            cleaned.append(p)
        return cleaned


class RoutingConfig(StrictModel):
    make_default: bool = True
    metric: int = Field(default=50, ge=1, le=10000)


class BodyField(StrictModel):
    """One field in a structured JSON body, built clickably in the UI.

    `path` is a dot-path into the JSON object (e.g. "signal.rsrp_dbm"); for a
    placeholder field, `value` is a placeholder name (e.g. "rsrp") resolved with
    its native type at send time and OMITTED if unknown — so the body is always
    valid JSON. For a static field, `value` is a literal."""

    path: str = Field(pattern=r"^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*$")
    value: str
    kind: Literal["placeholder", "static"] = "placeholder"


class MonitorRequest(StrictModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "HEAD"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    # Structured body builder (preferred). When non-empty, the JSON body is
    # assembled from these fields and `body` is ignored.
    body_fields: list[BodyField] = Field(default_factory=list)
    timeout_seconds: float = Field(default=15, gt=0, le=300)
    expect_status: list[int] = Field(default=[200, 204], min_length=1)


class MonitorSchedule(StrictModel):
    """Optional weekly time window that gates scheduled heartbeats, so probes
    only fire when someone is watching (e.g. Mon-Fri 9-6 Eastern). The window
    is opt-in (`enabled`); `override` forces sending on/off regardless. A manual
    "send now" always bypasses this. The pure decision lives in
    sim_monitor.monitor.schedule.is_active()."""

    enabled: bool = False
    timezone: str = "America/New_York"
    # Python weekday(): Monday=0 .. Sunday=6. Default is Mon-Fri.
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    start: str = Field(default="09:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="18:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # auto = follow the window; on = always send; off = never (scheduled) send.
    override: Literal["auto", "on", "off"] = "auto"

    @field_validator("days")
    @classmethod
    def _valid_days(cls, days: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in days):
            raise ValueError("schedule days must be 0 (Monday) .. 6 (Sunday)")
        return sorted(set(days))

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, tz: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {tz!r}") from e
        return tz


class MonitorConfig(StrictModel):
    enabled: bool = False
    interval_seconds: int = Field(default=300, ge=10)
    # Keep heartbeating over any available interface (ethernet/wifi) while
    # cellular is down, so the endpoint sees {status}=degraded instead of
    # silence. When false, probes pause until cellular reconnects.
    send_when_degraded: bool = True
    # Bind the probe socket to the cellular interface while connected, so a
    # success PROVES cellular egress. Set false when the endpoint is only
    # reachable via LAN/VPN (e.g. testing against a local server).
    bind_cellular: bool = True
    # Optional weekly window limiting when scheduled probes fire.
    schedule: MonitorSchedule = Field(default_factory=MonitorSchedule)
    request: MonitorRequest | None = None

    @model_validator(mode="after")
    def _enabled_requires_request(self) -> MonitorConfig:
        if self.enabled and self.request is None:
            raise ValueError("monitor.enabled is true but monitor.request is missing")
        return self


class FallbackTestConfig(StrictModel):
    airplane_seconds: int = Field(default=900, ge=10, le=7200)


class LatencyConfig(StrictModel):
    """Per-interface ICMP latency + packet-loss probing. Global (device-level),
    not per-profile: it pings each up interface (cellular + any wifi/ethernet)
    against the configured targets so a cellular-only problem can be told apart
    from a systemic one. Raw samples roll up into hourly/daily aggregates for
    long-term review."""

    enabled: bool = False
    interval_seconds: int = Field(default=60, ge=10)  # lower = denser, raise to throttle
    targets: list[str] = Field(
        default=["1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9"],
        min_length=1,
    )
    packet_count: int = Field(default=5, ge=1, le=20)  # pings per target per cycle
    timeout_seconds: int = Field(default=2, ge=1, le=30)
    # Interfaces to probe. Empty = auto-enumerate every up interface each cycle.
    interfaces: list[str] = Field(default_factory=list)
    exclude_interfaces: list[str] = Field(default_factory=list)
    raw_retention_days: int = Field(default=7, ge=1, le=90)
    rollup_retention_days: int = Field(default=30, ge=1, le=400)
    # Display-only: pin a chart colour per interface (e.g. {"wlan0": "#3b82f6"})
    # so the same interface looks identical across devices. Unset interfaces get
    # a deterministic colour from their name. Values are "#rrggbb".
    interface_colors: dict[str, str] = Field(default_factory=dict)

    @field_validator("interface_colors")
    @classmethod
    def _valid_hex_colors(cls, colors: dict[str, str]) -> dict[str, str]:
        for iface, hexval in colors.items():
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", hexval):
                raise ValueError(f"interface_colors[{iface}] must be #rrggbb, got {hexval!r}")
        return colors


SmsMatchType = Literal["contains", "exact", "prefix", "regex"]


class SmsReplyRule(StrictModel):
    """One auto-reply rule: when an inbound SMS body matches `pattern` (by the
    chosen `match` mode), the daemon sends `reply` back to the sender.

    Matching is case-insensitive by default. The pure decision lives in
    sim_monitor.core.sms_reply.find_reply()."""

    name: str = ""  # optional human label, shown in the UI / event log
    enabled: bool = True
    match: SmsMatchType = "contains"
    pattern: str = Field(min_length=1)
    case_sensitive: bool = False
    reply: str = Field(min_length=1, max_length=1600)  # ~10 SMS parts

    @model_validator(mode="after")
    def _valid_regex(self) -> SmsReplyRule:
        if self.match == "regex":
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"invalid regex {self.pattern!r}: {e}") from e
        return self


class SmsAutoReplyConfig(StrictModel):
    """Device-level SMS auto-responder: a list of pattern->reply rules tried in
    order (first match wins). Global, not per-profile. UI-managed (stored in the
    device DB under the 'sms_auto_reply' setting), so it can carry user message
    content and never needs to be committed."""

    enabled: bool = False
    rules: list[SmsReplyRule] = Field(default_factory=list)


def _validate_context_set(contexts: list[PdpContext], label: str) -> None:
    """Each context set must have unique CIDs and exactly one bearer (a single
    context is auto-promoted). Mutates `contexts` to set the implicit bearer."""
    cids = [c.cid for c in contexts]
    if len(set(cids)) != len(cids):
        raise ValueError(f"{label}: duplicate PDP context cids: {cids}")
    bearers = [c for c in contexts if c.bearer]
    if not bearers and len(contexts) == 1:
        contexts[0].bearer = True
        bearers = contexts
    if len(bearers) != 1:
        raise ValueError(f"{label}: exactly one PDP context must have bearer: true")


class PdpVariant(StrictModel):
    """An alternative PDP-context set. When a profile lists variants, the daemon
    tries each in order until one attaches + gets an IP. This covers cases where
    the right context can't be known from the ICCID alone — e.g. Verizon-direct
    SIMs whose PDP context depends on the Hologram data plan."""

    name: str = ""
    pdp_contexts: list[PdpContext] = Field(min_length=1, max_length=MAX_PDP_CONTEXTS)

    @model_validator(mode="after")
    def _validate(self) -> PdpVariant:
        _validate_context_set(self.pdp_contexts, f"variant {self.name or '?'}")
        return self


class Profile(StrictModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    description: str = ""
    match: MatchSpec = Field(default_factory=MatchSpec)
    pdp_contexts: list[PdpContext] = Field(min_length=1, max_length=MAX_PDP_CONTEXTS)
    # Optional alternative context sets tried (after pdp_contexts) until one
    # connects. Empty = just pdp_contexts.
    pdp_variants: list[PdpVariant] = Field(default_factory=list)
    at_init: list[str] = Field(default_factory=list)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)
    fallback_test: FallbackTestConfig = Field(default_factory=FallbackTestConfig)

    @model_validator(mode="after")
    def _validate_contexts(self) -> Profile:
        _validate_context_set(self.pdp_contexts, "pdp_contexts")
        return self

    @property
    def bearer_context(self) -> PdpContext:
        return next(c for c in self.pdp_contexts if c.bearer)

    def context_sets(self) -> list[tuple[str, list[PdpContext]]]:
        """All PDP-context sets to try, in order: the primary then each variant."""
        sets = [("default", self.pdp_contexts)]
        for i, v in enumerate(self.pdp_variants):
            sets.append((v.name or f"variant-{i + 1}", v.pdp_contexts))
        return sets


class WebConfig(StrictModel):
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


class ModemConfig(StrictModel):
    # "auto" = /dev/sim-monitor-at udev symlink, then known VID/interface hints.
    # Set an explicit device (e.g. /dev/ttyUSB3) to override.
    at_port: str = "auto"
    baud: int = Field(default=115200, ge=1200)


class DaemonConfig(StrictModel):
    tick_seconds: float = Field(default=5, gt=0, le=60)
    connect_timeout_seconds: int = Field(default=90, ge=10)
    # Total time allowed for network registration + IP before the supervisor
    # steps in. Roaming SIMs (Hologram) may scan carriers for minutes,
    # especially after a modem reset -- interrupting makes it WORSE.
    registration_timeout_seconds: int = Field(default=300, ge=30)
    # After the recovery ladder is exhausted on the matched profile, try a
    # built-in default (APN "hologram", IPv4) as a last-ditch catch-all so a
    # device is never stranded by a missing/wrong profile. Set false for strict
    # per-SIM control.
    fallback_to_default_profile: bool = True
    # How often to capture deep link telemetry (signal/serving-cell) for the
    # history charts, while connected.
    telemetry_interval_seconds: int = Field(default=30, ge=5)
    # Some modems (or a ModemManager race at boot) bring the data link up as a
    # legacy serial PPP link (ppp0) instead of the native wwan/QMI netdev. On
    # PPP the gateway is usually 0.0.0.0 and cellular can't win the default
    # route, so reset the modem to coax it back to native mode. Bounded by
    # ppp_reset_max_attempts so a modem that *only* does PPP doesn't reset-loop
    # forever -- after the cap it accepts the PPP link to stay online.
    reset_on_ppp_interface: bool = True
    ppp_reset_max_attempts: int = Field(default=2, ge=0)


class AppConfig(StrictModel):
    web: WebConfig = Field(default_factory=WebConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    modem: ModemConfig = Field(default_factory=ModemConfig)
    latency: LatencyConfig = Field(default_factory=LatencyConfig)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    db_path: Path = Path("sim-monitor.db")
    profiles_dir: Path = Path("config/profiles.d")
    simulate: bool = False
