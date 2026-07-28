"""Low-level USB HID transport for KTMicro chips."""

import glob
import hid as _hid

from typing import Optional, List


KT_VID = 0x31B2
KT_PID = 0x0111

RPT_ID   = 0x4B
RPT_SIZE = 11


class HIDTransport:
    """Raw HID communication over /dev/hidraw on Linux."""

    def __init__(self, vid: int = KT_VID, pid: int = KT_PID):
        self._vid = vid
        self._pid = pid
        self._dev = None

    # ---- discovery ----

    @staticmethod
    def list_devices() -> List[dict]:
        return list(_hid.enumerate())

    def find_hidraw(self) -> Optional[str]:
        for ue in sorted(glob.glob('/sys/class/hidraw/hidraw*/device/uevent')):
            try:
                with open(ue) as f:
                    content = f.read().upper()
                vid_str = f'{self._vid:04X}'
                pid_str = f'{self._pid:04X}'
                if vid_str in content and pid_str in content:
                    return '/dev/' + ue.split('/')[4]
            except Exception:
                continue
        return None

    # ---- open / close ----

    def open(self) -> bool:
        path = self.find_hidraw()
        if path is None:
            return False
        self._dev = _hid.device()
        try:
            self._dev.open_path(path.encode())
        except OSError:
            try:
                self._dev.open(self._vid, self._pid)
            except OSError:
                self._dev = None
                return False
        return True

    def close(self):
        if self._dev:
            self._dev.close()
            self._dev = None

    @property
    def is_open(self) -> bool:
        return self._dev is not None

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
        """Read feature report."""
        resp = self._dev.get_feature_report(0, length)
        return bytes(resp) if isinstance(resp, list) else resp
