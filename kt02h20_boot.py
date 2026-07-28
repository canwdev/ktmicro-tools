"""KT02H20 bootloader protocol — flash firmware programming.

Uses HID feature reports (not interrupt transfers).
Device must be in bootloader mode (different VID:PID than run mode).
"""

import struct
import time

from typing import Optional, Tuple

from hid_transport import HIDTransport, KT_VID


# ---- bootloader protocol constants ----

CMD_SYNC  = 0x1E4B544D  # ".KTM"
CMD_KEY   = 0xF04B4559  # unlock
CMD_VER   = 0xF0564552  # version query
CMD_CHP   = 0xD2434850  # chip ID query
CMD_CFG   = 0x2D        # flash config
CMD_PWO   = 0x3C50574F  # power on
CMD_KSTA  = 0x4B535441  # start
CMD_STP   = 0x96535450  # stop
CMD_WRITE = 0x69        # write block

BL_ACK  = 0x78
BL_CONT = 0xA5

BLOCK_SIZE = 512


class KT02H20Boot:
    """KT02H20 flash programming via bootloader."""

    def __init__(self, vid: int = KT_VID, pid: int = 0x0101):
        self._hid = HIDTransport(vid, pid)

    def open(self) -> bool:
        return self._hid.open()

    def close(self):
        self._hid.close()

    @property
    def connected(self) -> bool:
        return self._hid.is_open

    # ---- context manager ----

    def __enter__(self):
        if not self.open():
            raise ConnectionError("KT02H20 bootloader device not found")
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- protocol steps ----

    def sync(self, retries: int = 5) -> bool:
        cmd = struct.pack('<I', CMD_SYNC)
        for _ in range(retries):
            try:
                self._hid.send_feature(cmd)
                resp = self._hid.recv_feature()
                if resp and len(resp) >= 2 and resp[1] == BL_ACK:
                    return True
            except OSError:
                pass
            time.sleep(0.2)
        return False

    def version(self) -> Optional[Tuple[int, int]]:
        self._hid.send_feature(struct.pack('<I', CMD_VER))
        resp = self._hid.recv_feature()
        if resp and len(resp) >= 3 and resp[1] == BL_ACK:
            return (resp[2], resp[3] if len(resp) > 3 else 0)
        return None

    def key(self) -> bool:
        self._hid.send_feature(struct.pack('<I', CMD_KEY))
        resp = self._hid.recv_feature()
        return resp is not None and len(resp) >= 2 and resp[1] == BL_ACK

    def chip_id(self) -> Optional[str]:
        self._hid.send_feature(struct.pack('<I', CMD_CHP))
        resp = self._hid.recv_feature()
        if resp and len(resp) >= 6 and resp[4] == 0x12:  # 0x12 = chip-ID response marker
            end = resp.find(b'\x00', 5)
            if end < 0:
                end = len(resp) - 1
            return resp[5:end].decode('ascii', errors='replace')
        return None

    def configure(self, chip_type: int = 0x10, flash_base: int = 0x6000,
                  sector_size: int = 0x0E, timing: int = 0x15) -> bool:
        cmd = bytes([CMD_CFG, 0x29, 0x00, chip_type, sector_size, timing,
                     flash_base & 0xFF, (flash_base >> 8) & 0xFF, 0x00, 0xBC])
        self._hid.send_feature(cmd)
        resp = self._hid.recv_feature()
        return resp is not None and len(resp) >= 2 and resp[1] == BL_ACK

    def power_on(self) -> bool:
        self._hid.send_feature(struct.pack('<I', CMD_PWO))
        resp = self._hid.recv_feature()
        return resp is not None and len(resp) >= 2 and resp[1] == BL_ACK

    def start(self) -> bool:
        self._hid.send_feature(struct.pack('<I', CMD_KSTA))
        resp = self._hid.recv_feature()
        return resp is not None and len(resp) >= 2 and resp[1] == BL_ACK

    def stop(self):
        self._hid.send_feature(struct.pack('<I', CMD_STP))

    def write_block(self, block_addr: int, data: bytes,
                    subtype: int = 0x00, region: int = 0x00E4) -> bool:
        padded = data.ljust(BLOCK_SIZE, b'\x00')
        header = struct.pack('<BBHH', CMD_WRITE, subtype,
                             region & 0xFFFF, block_addr & 0xFFFF)
        self._hid.send_feature(header + padded)
        resp = self._hid.recv_feature()
        if resp and resp[1] in (BL_ACK, BL_CONT):
            if resp[1] == BL_CONT:
                return True
            try:
                resp2 = self._hid.recv_feature()
                return resp2 is not None and resp2[1] in (BL_ACK, BL_CONT)
            except Exception:
                return True
        return False

    # ---- full flash sequence ----

    def flash(self, fw_path: str, chip_type: int = 0x10) -> bool:
        with open(fw_path, 'rb') as f:
            fw = f.read()
        size = len(fw)
        print(f"Firmware: {size} bytes")

        if not self.sync():       print("FAIL: sync");    return False
        print("Bootloader:", self.version())

        if not self.key():        print("FAIL: key");     return False
        cid = self.chip_id()
        if not cid:               print("FAIL: chip ID"); return False
        print(f"Chip: {cid}")

        if not self.configure(chip_type): print("FAIL: config"); return False
        if not self.power_on():          print("FAIL: pwr");     return False
        if not self.start():             print("FAIL: start");   return False

        ba = 0x80
        meta = _make_meta(cid, size)
        if not self.write_block(ba, meta, subtype=0xF0):
            print("FAIL: meta"); return False
        ba += 4

        n = (size + BLOCK_SIZE - 1) // BLOCK_SIZE
        for i in range(0, size, BLOCK_SIZE):
            bn = i // BLOCK_SIZE + 1
            if not self.write_block(ba, fw[i:i + BLOCK_SIZE]):
                print(f"FAIL: block {bn}"); return False
            ba += 4
            print(f"  block {bn}/{n}", end='\r')
        print(f"\n  {n} blocks written")

        if not self.write_block(ba, b'\x00' * BLOCK_SIZE,
                                subtype=0x90, region=0x00E2):
            print("FAIL: config block"); return False
        sig = _make_sig(cid, fw)
        if not self.write_block(0x80, sig, subtype=0x10, region=0x00E0):
            print("FAIL: signature"); return False

        self.stop()
        print("Done")
        return True


# ---- helpers ----

def _make_meta(chip_id: str, fw_size: int) -> bytes:
    m = bytearray(BLOCK_SIZE)
    cid = chip_id.encode('ascii')
    m[0:len(cid)] = cid
    struct.pack_into('<I', m, 8, fw_size)
    m[12:16] = b'Size'; m[48:52] = b'ENTY'
    m[72:92] = b'2024-01-01-0000-0000-0000'
    m[120:128] = b'PerfCfg:\x00'; m[128:130] = b'\x04\x00'
    m[136:144] = b'KTMicro\x00'
    m[144:160] = b'USB-C Audio\x00\x00\x00'
    m[184:200] = bytes([4,3,9,4,0x12,1,0,2,0,0,0,0x40,0xB2,0x31,1,0])
    return bytes(m)


def _make_sig(chip_id: str, fw: bytes) -> bytes:
    if '02H2' in chip_id or '02H20' in chip_id:
        name = b'KT_lnv1b_flash_1'
    elif 'MSV2B' in chip_id.upper():
        name = b'KT_msv2b_flash_1'
    elif '0712' in chip_id:
        name = b'KTM_TT_V3_flash_1'
    else:
        name = b'KT_lnv1b_flash_1'
    s = bytearray(64)
    s[0:len(name)] = name
    s[len(name)] = 0
    struct.pack_into('<I', s, len(name) + 1, _crc32(fw))
    return bytes(s)


# CRC32

_CRC_TABLE = None


def _crc32_table():
    global _CRC_TABLE
    if _CRC_TABLE is None:
        _CRC_TABLE = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0xEDB88320 if c & 1 else c >> 1
            _CRC_TABLE.append(c)
    return _CRC_TABLE


def _crc32(data: bytes) -> int:
    c = 0xFFFFFFFF
    for b in data:
        c = _crc32_table()[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF
