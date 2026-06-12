"""Bounded subprocess execution for system tools (nmcli, mmcli, ip, udevadm).

Every call has a timeout so a wedged tool can never stall the daemon past the
systemd watchdog window. Failures raise BackendError with stderr attached.
"""

from __future__ import annotations

import logging
import subprocess

from sim_monitor.system.backend import BackendError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0


def run(args: list[str], timeout: float = DEFAULT_TIMEOUT) -> str:
    log.debug("exec: %s", " ".join(args))
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as e:
        raise BackendError(f"{args[0]} not installed: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"{args[0]} timed out after {timeout}s") from e
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BackendError(f"{' '.join(args[:3])} failed (rc={result.returncode}): {detail}")
    return result.stdout
