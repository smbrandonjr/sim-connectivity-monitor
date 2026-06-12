"""USB power-cycle for a wedged modem: toggle the sysfs `authorized` flag.

This de-enumerates and re-enumerates the device — the firmware reboots and
ModemManager re-detects it. Last rung of the recovery ladder before parking.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sim_monitor.system.backend import BackendError

log = logging.getLogger(__name__)


def usb_device_dir_for_tty(tty_path: str, sysfs_root: Path = Path("/sys")) -> Path:
    """Walk up from a tty's sysfs node to the USB *device* (has `authorized`)."""
    name = Path(tty_path).resolve().name  # ttyUSB3, symlinks resolved
    node = (sysfs_root / "class" / "tty" / name / "device").resolve()
    for candidate in (node, *node.parents):
        if (candidate / "authorized").is_file() and (candidate / "idVendor").is_file():
            return candidate
    raise BackendError(f"no USB device with 'authorized' found above {tty_path}")


def power_cycle_tty(
    tty_path: str, off_seconds: float = 3.0, sysfs_root: Path = Path("/sys")
) -> None:
    device = usb_device_dir_for_tty(tty_path, sysfs_root)
    authorized = device / "authorized"
    log.warning("USB power-cycling %s (%s)", tty_path, device.name)
    try:
        authorized.write_text("0")
        time.sleep(off_seconds)
        authorized.write_text("1")
    except OSError as e:
        raise BackendError(f"USB power-cycle failed: {e}") from e
