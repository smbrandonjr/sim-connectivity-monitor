import os

import pytest

from sim_monitor.system.backend import BackendError
from sim_monitor.system.usb_power import power_cycle_tty, usb_device_dir_for_tty


def build_fake_sysfs(tmp_path):
    """Recreate the sysfs shape: class/tty/ttyUSB3/device -> the interface dir,
    whose parent USB device holds `authorized` + `idVendor`. (The real interface
    dir is named like '1-1.2:1.3'; Windows forbids ':' so we use '_' — the code
    never inspects the name.)"""
    usb_dev = tmp_path / "devices" / "usb1" / "1-1" / "1-1.2"
    iface = usb_dev / "1-1.2_1.3"
    tty_node = iface / "ttyUSB3"
    tty_node.mkdir(parents=True)
    (usb_dev / "authorized").write_text("1")
    (usb_dev / "idVendor").write_text("2c7c")
    class_tty = tmp_path / "class" / "tty" / "ttyUSB3"
    class_tty.mkdir(parents=True)
    try:
        os.symlink(iface, class_tty / "device", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable (Windows without developer mode)")
    return usb_dev


def test_finds_usb_device_above_tty(tmp_path):
    usb_dev = build_fake_sysfs(tmp_path)
    found = usb_device_dir_for_tty("/dev/ttyUSB3", sysfs_root=tmp_path)
    assert found == usb_dev.resolve()


def test_power_cycle_toggles_authorized(tmp_path):
    usb_dev = build_fake_sysfs(tmp_path)
    power_cycle_tty("/dev/ttyUSB3", off_seconds=0, sysfs_root=tmp_path)
    assert (usb_dev / "authorized").read_text() == "1"


def test_missing_device_raises(tmp_path):
    (tmp_path / "class" / "tty").mkdir(parents=True)
    with pytest.raises(BackendError):
        usb_device_dir_for_tty("/dev/ttyUSB9", sysfs_root=tmp_path)
