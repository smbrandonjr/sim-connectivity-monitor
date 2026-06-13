"""Cheap, dependency-free host metrics for heartbeat payloads (Linux /proc;
empty dict on other platforms so the fields are simply omitted)."""

from __future__ import annotations

from pathlib import Path


def collect_host_metrics() -> dict:
    out: dict = {}
    _uptime(out)
    _loadavg(out)
    _meminfo(out)
    _temperature(out)
    return out


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def _uptime(out: dict) -> None:
    text = _read("/proc/uptime")
    if text:
        try:
            out["uptime_s"] = int(float(text.split()[0]))
        except (ValueError, IndexError):
            pass


def _loadavg(out: dict) -> None:
    text = _read("/proc/loadavg")
    if text:
        try:
            out["cpu_load"] = float(text.split()[0])
        except (ValueError, IndexError):
            pass


def _meminfo(out: dict) -> None:
    text = _read("/proc/meminfo")
    if not text:
        return
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                out["mem_free_mb"] = int(int(line.split()[1]) / 1024)
            except (ValueError, IndexError):
                pass
            return


def _temperature(out: dict) -> None:
    text = _read("/sys/class/thermal/thermal_zone0/temp")
    if text:
        try:
            out["temperature_c"] = round(int(text.strip()) / 1000, 1)
        except ValueError:
            pass
