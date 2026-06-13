"""Pydantic models for the app config and configuration profiles.

Everything user-editable (config.yaml, profiles.d/*.yaml) is validated through
these models so YAML mistakes surface as readable errors instead of runtime
failures deep in the daemon.
"""

from __future__ import annotations

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


class MonitorRequest(StrictModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "HEAD"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = ""
    timeout_seconds: float = Field(default=15, gt=0, le=300)
    expect_status: list[int] = Field(default=[200, 204], min_length=1)


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
    request: MonitorRequest | None = None

    @model_validator(mode="after")
    def _enabled_requires_request(self) -> MonitorConfig:
        if self.enabled and self.request is None:
            raise ValueError("monitor.enabled is true but monitor.request is missing")
        return self


class FallbackTestConfig(StrictModel):
    airplane_seconds: int = Field(default=900, ge=10, le=7200)


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


class AppConfig(StrictModel):
    web: WebConfig = Field(default_factory=WebConfig)
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    modem: ModemConfig = Field(default_factory=ModemConfig)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    db_path: Path = Path("sim-monitor.db")
    profiles_dir: Path = Path("config/profiles.d")
    simulate: bool = False
