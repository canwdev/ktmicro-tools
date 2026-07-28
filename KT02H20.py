"""KT02H20 DSP register access in run mode.

Protocol (verified from USB captures):
  Write: 4B [addr:LE32] 57 00 [val:LE32]  → ACK 0x03 at response byte 7
  Read:  4B [addr:LE32] 52 00 00 00 00 00 → value at response bytes 7-10
"""

import struct
import time

from typing import Optional, Dict

from hid_transport import HIDTransport, KT_VID, KT_PID, RPT_ID

CMD_READ  = 0x52
CMD_WRITE = 0x57


# ---- Register map (validated against KT_USB_APP.exe disassembly) ----

REG_MARKER     = 0x00
REG_FLAGS      = 0x01   # "Protocol" per app UI: model/protocol version code;
                        # bit9 (0x0200) = single-DAC model (app shows one DAC control)
REG_VERSION    = 0x04
REG_TIMESTAMP  = 0x08
REG_MANUF      = 0x40
REG_PRODUCT    = 0x48
REG_SERIAL     = 0x50
REG_VIDPID     = 0x5B   # [PID:16][VID:16] packed
REG_MAGIC      = 0xE1

# EQ: each channel block = 12 regs (enable at base+0, 5 band pairs at base+2)
REG_EQ_ADC_EN   = 0x18  # bit0 = ADC EQ enable
REG_EQ_BASE     = 0x1A  # ADC EQ bands 0-4, stride 2 (A_reg,B_reg pairs)
REG_EQ_DAC_EN   = 0x24  # bit0 = DAC EQ enable
REG_EQ_DAC_BASE = 0x26  # DAC EQ bands 0-4, stride 2
REG_EQ_STRIDE   = 2
REG_EQ_BANDS    = 5

# Volume
REG_PGA_ADC    = 0x3A   # analog PGA gain index (A_ADC)
REG_PGA_DAC    = 0x3B   # analog PGA gain index (A_DAC)
REG_DIG_ADC    = 0x65   # byte0 = gain_dB x 2 (0.5 dB steps)
REG_DIG_DAC    = 0x66   # byte0 = DACL, byte1 = DACR (same encoding)

# Analog PGA gain tables (comboBox index -> gain), from the app UI
# A_ADC: 8 static items        (verified: idx 3 = 14 dB)
# A_DAC: mute + 15 computed    (verified: idx 9 = -6 dB)
#        text(i) = 1.5*(i-1) - 18 dB for i=1..15
PGA_ADC_GAINS = ['0 dB', '-6 dB', '8 dB', '14 dB', '20 dB', '26 dB', '32 dB', '44 dB']
PGA_DAC_GAINS = ['mute'] + [f"{1.5*(i-1) - 18:+.1f} dB" for i in range(1, 16)]

# DRC
REG_NG_BASE    = 0x71   # 0x71-0x73 NoiseGate
REG_LIM_BASE   = 0x78   # 0x78-0x79 Limiter


def _read_str(regs: Dict[int, int], start: int, count: int) -> str:
    """Read null-terminated ASCII string from consecutive LE32 registers."""
    chars = []
    for i in range(count):
        val = regs.get(start + i)
        if val is None:
            break
        for b in struct.pack('<I', val):
            if 32 <= b < 127:
                chars.append(chr(b))
    return ''.join(chars).rstrip('\x00')


class KT02H20:
    """KT02H20 DSP configuration (run mode)."""

    def __init__(self, vid: int = KT_VID, pid: int = KT_PID):
        self._hid = HIDTransport(vid, pid)

    # ---- connection ----

    def open(self) -> bool:
        return self._hid.open()

    def close(self):
        self._hid.close()

    @property
    def connected(self) -> bool:
        return self._hid.is_open

    # ---- read / write ----

    def read(self, addr: int) -> Optional[int]:
        """Read 32-bit register via 0x52 command."""
        tx = bytes([RPT_ID]) \
             + struct.pack('<I', addr) \
             + bytes([CMD_READ, 0, 0, 0, 0, 0])
        self._hid.write_report(tx)
        time.sleep(0.005)
        rx = self._hid.read_report()
        if rx and len(rx) >= 11 and rx[5] == CMD_READ:
            return rx[7] | (rx[8] << 8) | (rx[9] << 16) | (rx[10] << 24)
        return None

    def write(self, addr: int, val: int) -> bool:
        """Write 32-bit register via 0x57 command. Returns True if ACKed."""
        tx = bytes([RPT_ID]) \
             + struct.pack('<I', addr) \
             + bytes([CMD_WRITE, 0]) \
             + struct.pack('<I', val)
        self._hid.write_report(tx)
        time.sleep(0.005)
        rx = self._hid.read_report()
        return rx is not None and len(rx) >= 8 and rx[5] == CMD_WRITE and rx[7] == 0x03

    def dump(self) -> Dict[int, int]:
        """Read all 256 registers (0x00-0xFF)."""
        return {a: self.read(a) for a in range(0x100) if self.read(a) is not None}

    # ---- high-level info ----

    def info(self) -> dict:
        """Read device identification."""
        r = {}
        for start, count in [(REG_VERSION, 2), (REG_MANUF, 8),
                              (REG_PRODUCT, 8), (REG_SERIAL, 7)]:
            vals = {}
            for a in range(start, min(start + count, 0x100)):
                v = self.read(a)
                if v is not None:
                    vals[a] = v
            if vals:
                r[start] = _read_str(vals, start, count)

        for a in [REG_VIDPID, REG_PGA_ADC, REG_PGA_DAC, REG_MARKER]:
            v = self.read(a)
            if v is not None:
                r[a] = v
        return r

    def state(self) -> dict:
        """Read complete DSP state: ID, EQ bands, DRC, config."""
        s = {}

        # Identification
        s['product']  = _read_str({a: self.read(a) for a in range(REG_PRODUCT, REG_PRODUCT + 8)}, REG_PRODUCT, 8)
        s['vendor']   = _read_str({a: self.read(a) for a in range(REG_MANUF, REG_MANUF + 8)}, REG_MANUF, 8)
        s['serial']   = _read_str({a: self.read(a) for a in range(REG_SERIAL, REG_SERIAL + 7)}, REG_SERIAL, 7)
        s['version']  = _read_str({a: self.read(a) for a in range(REG_VERSION, REG_VERSION + 2)}, REG_VERSION, 2)
        s['vidpid']   = self.read(REG_VIDPID)
        s['magic']    = self.read(REG_MAGIC)

        # Volume: analog PGA indices + digital gains
        s['chip_flags'] = self.read(REG_FLAGS)
        s['pga_adc'] = self.read(REG_PGA_ADC)
        s['pga_dac'] = self.read(REG_PGA_DAC)
        s['dig_adc'] = self.read(REG_DIG_ADC)
        s['dig_dac'] = self.read(REG_DIG_DAC)

        # EQ enables (bit0)
        s['eq_adc_en'] = self.read(REG_EQ_ADC_EN)
        s['eq_dac_en'] = self.read(REG_EQ_DAC_EN)

        # EQ bands (raw A/B register pairs per channel)
        def _bands(base):
            out = []
            for i in range(REG_EQ_BANDS):
                addr = base + i * REG_EQ_STRIDE
                out.append({
                    'index': i,
                    'addr_a': hex(addr),
                    'val_a': self.read(addr),
                    'addr_b': hex(addr + 1),
                    'val_b': self.read(addr + 1),
                })
            return out
        s['eq_bands']     = _bands(REG_EQ_BASE)      # ADC
        s['eq_dac_bands'] = _bands(REG_EQ_DAC_BASE)  # DAC

        # DRC group (only 0x71-0x73 NoiseGate and 0x78-0x79 Limiter are
        # device-backed; Compander/Expander/AGC are UI-only in the app)
        drc = {}
        for a in list(range(REG_NG_BASE, REG_NG_BASE + 3)) + \
                list(range(REG_LIM_BASE, REG_LIM_BASE + 2)):
            v = self.read(a)
            if v is not None:
                drc[a] = v
        s['drc'] = drc

        return s
