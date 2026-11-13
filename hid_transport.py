"""Low-level USB HID transport for KTMicro chips."""

import glob
import os

import hid as _hid

from typing import Optional, List


KT_VID = 0x31B2
KT_PID = 0x0111          # PID documented for run mode

# The DSP protocol lives on HID interface 3 of the composite device.
# Real units also enumerate under other PIDs (e.g. 0x2038), so discovery
# treats the PID as a hint rather than a hard requirement.
KT_INTERFACE = 3

RPT_ID   = 0x4B
RPT_SIZE = 11


class HIDTransport:
    """Raw HID communication with KTMicro chips (Linux hidraw / Windows HID)."""

    def __init__(self, vid: int = KT_VID, pid: int = KT_PID):
        self._vid = vid
        self._pid = pid
        self._dev = None
        self._path = None
        self._info = None

    # ---- discovery ----

    @staticmethod
    def list_devices() -> List[dict]:
        return list(_hid.enumerate())

    def find_hidraw(self) -> Optional[str]:
        """Scan Linux sysfs for any hidraw node of this VID (PID-agnostic)."""
        vid_str = f'{self._vid:04X}'
        for ue in sorted(glob.glob('/sys/class/hidraw/hidraw*/device/uevent')):
            try:
                with open(ue) as f:
                    content = f.read().upper()
            except (OSError, PermissionError):
                continue
            if vid_str in content:
                return '/dev/' + os.path.basename(os.path.dirname(ue))
        return None

    def find_device(self) -> Optional[dict]:
        """Pick the HID interface that carries the protocol.

        Preference: exact VID+PID match, then interface 3, then any
        device of this vendor (covers undocumented PIDs like 0x2038).
        """
        devs = [d for d in self.list_devices()
                if d.get('vendor_id') == self._vid]
        if not devs:
            return None
        exact = [d for d in devs if d.get('product_id') == self._pid]
        if exact:
            return exact[0]
        iface = [d for d in devs
                 if d.get('interface_number') == KT_INTERFACE]
        return (iface or devs)[0]

    # ---- open / close ----

    def _open_path(self, path) -> bool:
        if isinstance(path, str):
            path = path.encode()
        dev = _hid.device()
        try:
            dev.open_path(path)
        except OSError:
            return False
        self._dev = dev
        self._path = path.decode(errors='replace') \
            if isinstance(path, bytes) else path
        return True

    def open(self) -> bool:
        self.close()

        # 1) enumerate via hidapi: works on Linux (hidraw path) and
        #    Windows (\\?\HID#... device path)
        found = self.find_device()
        if found and found.get('path'):
            if self._open_path(found['path']):
                self._pid = found.get('product_id', self._pid)
                self._info = found
                return True

        # 2) Linux fallback: raw sysfs scan by VID
        hidraw = self.find_hidraw()
        if hidraw and self._open_path(hidraw):
            return True

        # 3) last resort: open by VID/PID (first matching interface)
        try:
            dev = _hid.device()
            dev.open(self._vid, self._pid)
        except OSError:
            self._dev = None
            self._path = None
            return False
        self._dev = dev
        self._path = f"{self._vid:04X}:{self._pid:04X}"
        return True

    def close(self):
        if self._dev:
            self._dev.close()
            self._dev = None
        self._info = None

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    @property
    def path(self) -> Optional[str]:
        """HID device path used for the current connection (e.g. /dev/hidraw3)."""
        return self._path

    # ---- context manager ----

    def __enter__(self):
        if not self.open():
            raise ConnectionError(
                f"KTMicro device VID=0x{self._vid:04X} (HID interface "
                f"{KT_INTERFACE}) not found")
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @property
    def device_info(self) -> Optional[dict]:
        """Enumeration record of the device that got opened (None if closed)."""
        return self._info

    # ---- interrupt transfers (run-mode DSP) ----

    def write_report(self, data: bytes):
        """Send 11-byte HID output report."""
        self._dev.write(data)

    def read_report(self, timeout_ms: int = 500) -> Optional[bytes]:
        """Read 11-byte HID input report."""
        try:
            rx = self._dev.read(RPT_SIZE, timeout_ms=timeout_ms)
            return bytes(rx) if rx else None
        except OSError:
            return None

    # ---- feature reports (bootloader mode) ----

    def send_feature(self, data: bytes):
        """Send feature report (prepends report ID 0)."""
        self._dev.send_feature_report(b'\x00' + data)

    def recv_feature(self, length: int = 64) -> Optional[bytes]:
        """Read feature report. Returns None on timeout, bytes otherwise."""
        resp = self._dev.get_feature_report(0, length)
        if resp is None:
            return None
        return bytes(resp)
