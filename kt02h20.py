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

# Filter type maps
FILTER_TYPES = {0: 'P', 1: 'LPF', 2: 'HPF', 3: 'LS', 4: 'HS'}
TYPE_NAMES   = {'P': 0, 'LPF': 1, 'HPF': 2, 'LS': 3, 'HS': 4}


# ── Encoding / decoding (pure functions, no I/O) ──────────────

NG_FLAGS = 0x3C   # bits 0-6 in byte3 of reg 0x71 (module ID, hold time, etc)

def _ng_byte_db(dB: int) -> int:
    """Encode dB threshold as uint8:  256 + dB."""
    v = 256 + dB
    if not (0 <= v <= 255):
        raise ValueError(f"dB value {dB:+d} maps to byte {v}, must be in range -256..-1")
    return v

def _ng_byte_to_db(b: int) -> int:
    """Decode uint8 threshold → dB."""
    return b - 256

def ng_encode_71(en: bool, gatevol_dB: int, thhigh_dB: int, thlow_dB: int) -> int:
    """Encode register 0x71 from NG parameters."""
    b3 = NG_FLAGS | (0x80 if en else 0x00)
    return (b3 << 24) | (_ng_byte_db(gatevol_dB) << 16) | (_ng_byte_db(thhigh_dB) << 8) | _ng_byte_db(thlow_dB)

def ng_decode_71(v: int) -> dict:
    """Decode register 0x71 → {en, gatevol, thhigh, thlow, flags}."""
    return {
        'en':       bool((v >> 24) & 0x80),
        'flags':    (v >> 24) & 0x7F,
        'gatevol':  _ng_byte_to_db((v >> 16) & 0xFF),
        'thhigh':   _ng_byte_to_db((v >> 8) & 0xFF),
        'thlow':    _ng_byte_to_db(v & 0xFF),
    }

def ng_encode_72(rt_ms: int, at_ms: int) -> int:
    return ((rt_ms & 0xFFFF) << 16) | (at_ms & 0xFFFF)

def ng_decode_72(v: int) -> dict:
    return {'rt': (v >> 16) & 0xFFFF, 'at': v & 0xFFFF}

def ng_encode_73(noiseT_ms: int) -> int:
    return ((noiseT_ms & 0xFFFF) << 16) | 0x000A

def ng_decode_73(v: int) -> dict:
    return {'noiset': (v >> 16) & 0xFFFF, 'hold': v & 0xFFFF}

def lim_encode_78(en: bool, soft: bool, threshold_dB: int) -> int:
    b3 = 0
    if en:   b3 |= 0x80
    if soft: b3 |= 0x40
    return (b3 << 24) | _ng_byte_db(threshold_dB)

def lim_decode_78(v: int) -> dict:
    b0 = v & 0xFF
    b3 = (v >> 24) & 0xFF
    return {
        'en':        bool(b3 & 0x80),
        'soft':      bool(b3 & 0x40),
        'point':     b0 - 256,
    }

def lim_encode_79(rt_ms: int, at_ms: int) -> int:
    return ((rt_ms & 0xFFFF) << 16) | (at_ms & 0xFFFF)

def lim_decode_79(v: int) -> dict:
    return {'rt': (v >> 16) & 0xFFFF, 'at': v & 0xFFFF}

def eq_encode_a(freq_hz: int, gain_db: float) -> int:
    g10 = int(round(gain_db * 10)) & 0xFFFF
    return ((freq_hz & 0xFFFF) << 16) | g10

def eq_encode_b(ftype: int, q: float) -> int:
    q1000 = int(round(q * 1000)) & 0xFFFF
    return ((ftype & 0x7) << 16) | q1000

def _eq_decode_a(va: int) -> tuple:
    """Decode A register → (freq_hz, gain_db)."""
    freq = (va >> 16) & 0xFFFF
    lo16 = va & 0xFFFF
    if lo16 >= 0x8000:
        lo16 -= 0x10000
    return freq, lo16 / 10.0

def _eq_decode_b(vb: int) -> tuple:
    """Decode B register → (ftype_int, q)."""
    ftype = (vb >> 16) & 0x7
    q = (vb & 0xFFFF) / 1000.0 if vb else 0.0
    return ftype, q

def dig_encode(db: float) -> int:
    """Digital gain dB -> register byte (signed, 0.5 dB steps)."""
    v = int(round(db * 2))
    if not (-128 <= v <= 127):
        raise ValueError(f"digital gain {db} dB out of range (-64..+63.5 dB)")
    return v & 0xFF

def dig_decode(b: int) -> float:
    """Register byte -> dB (app uses movsx: byte is SIGNED, x0.5)."""
    if b >= 0x80:
        b -= 0x100
    return b / 2.0


def _decode_ascii(regs: Dict[int, int], start: int, count: int) -> str:
    """Decode printable ASCII chars from consecutive LE32 register values."""
    chars = []
    for i in range(count):
        val = regs.get(start + i)
        if val is None:
            break
        for b in struct.pack('<I', val):
            if 32 <= b < 127:
                chars.append(chr(b))
    return ''.join(chars).rstrip('\x00')


# ── Device class ───────────────────────────────────────────────

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

    def __enter__(self):
        if not self.open():
            raise ConnectionError("KT02H20 device not found")
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- raw register read / write ----

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
        """Read all 256 registers (0x00-0xFF). Returns only responding registers."""
        result = {}
        for a in range(0x100):
            v = self.read(a)
            if v is not None:
                result[a] = v
        return result

    # ---- device info ----

    def get_protocol_flags(self) -> Optional[int]:
        """Read register 0x01 (protocol/model code)."""
        return self.read(REG_FLAGS)

    def is_single_dac(self) -> bool:
        """True if register 0x01 bit9 (0x0200) indicates single-DAC model."""
        flags = self.get_protocol_flags()
        return bool(flags is not None and (flags & 0x200))

    # ---- NoiseGate ----

    def get_ng(self) -> Optional[dict]:
        """Read NoiseGate params: {en, gatevol, thhigh, thlow, rt, at, noiset}."""
        v71 = self.read(REG_NG_BASE)
        v72 = self.read(REG_NG_BASE + 1)
        v73 = self.read(REG_NG_BASE + 2)
        if v71 is None or v72 is None or v73 is None:
            return None
        d71 = ng_decode_71(v71)
        d72 = ng_decode_72(v72)
        d73 = ng_decode_73(v73)
        return {**d71, **d72, **d73}

    def set_ng(self, *, en=None, gatevol=None, thhigh=None, thlow=None,
               rt=None, at=None, noiset=None) -> bool:
        """Write NoiseGate params. None = keep current value."""
        cur = self.get_ng()
        if cur is None:
            return False
        p = {**cur,
             **{k: v for k, v in [('en', en), ('gatevol', gatevol),
                                    ('thhigh', thhigh), ('thlow', thlow),
                                    ('rt', rt), ('at', at), ('noiset', noiset)]
                if v is not None}}
        ok = True
        ok &= self.write(REG_NG_BASE, ng_encode_71(p['en'], p['gatevol'], p['thhigh'], p['thlow']))
        ok &= self.write(REG_NG_BASE + 1, ng_encode_72(p['rt'], p['at']))
        ok &= self.write(REG_NG_BASE + 2, ng_encode_73(p['noiset']))
        return ok

    # ---- Limiter ----

    def get_lim(self) -> Optional[dict]:
        """Read Limiter params: {en, soft, point, rt, at}."""
        v78 = self.read(REG_LIM_BASE)
        v79 = self.read(REG_LIM_BASE + 1)
        if v78 is None or v79 is None:
            return None
        d78 = lim_decode_78(v78)
        d79 = lim_decode_79(v79)
        return {**d78, **d79}

    def set_lim(self, *, en=None, soft=None, point=None, rt=None, at=None) -> bool:
        """Write Limiter params. None = keep current value."""
        cur = self.get_lim()
        if cur is None:
            return False
        p = {**cur,
             **{k: v for k, v in [('en', en), ('soft', soft), ('point', point),
                                    ('rt', rt), ('at', at)]
                if v is not None}}
        ok = True
        ok &= self.write(REG_LIM_BASE, lim_encode_78(p['en'], p['soft'], p['point']))
        ok &= self.write(REG_LIM_BASE + 1, lim_encode_79(p['rt'], p['at']))
        return ok

    # ---- Volume: analog PGA ----

    def get_pga_adc(self) -> Optional[int]:
        """Read A_ADC PGA gain index (0-7)."""
        return self.read(REG_PGA_ADC)

    def set_pga_adc(self, idx: int) -> bool:
        if not (0 <= idx < len(PGA_ADC_GAINS)):
            raise ValueError(f"PGA ADC index {idx} out of range 0..{len(PGA_ADC_GAINS)-1}")
        return self.write(REG_PGA_ADC, idx)

    def get_pga_dac(self) -> Optional[int]:
        """Read A_DAC PGA gain index (0-15)."""
        return self.read(REG_PGA_DAC)

    def set_pga_dac(self, idx: int) -> bool:
        if not (0 <= idx < len(PGA_DAC_GAINS)):
            raise ValueError(f"PGA DAC index {idx} out of range 0..{len(PGA_DAC_GAINS)-1}")
        return self.write(REG_PGA_DAC, idx)

    # ---- Volume: digital gains ----

    def get_dig_adc(self) -> Optional[float]:
        """Read DIG_ADC gain in dB."""
        v = self.read(REG_DIG_ADC)
        return dig_decode(v & 0xFF) if v is not None else None

    def set_dig_adc(self, db: float) -> bool:
        cur = self.read(REG_DIG_ADC) or 0
        return self.write(REG_DIG_ADC, (cur & ~0xFF) | dig_encode(db))

    def get_dig_dacl(self) -> Optional[float]:
        """Read DIG_DACL gain in dB (byte0 of 0x66)."""
        v = self.read(REG_DIG_DAC)
        return dig_decode(v & 0xFF) if v is not None else None

    def set_dig_dacl(self, db: float) -> bool:
        cur = self.read(REG_DIG_DAC) or 0
        return self.write(REG_DIG_DAC, (cur & ~0xFF) | dig_encode(db))

    def get_dig_dacr(self) -> Optional[float]:
        """Read DIG_DACR gain in dB (byte1 of 0x66)."""
        v = self.read(REG_DIG_DAC)
        return dig_decode((v >> 8) & 0xFF) if v is not None else None

    def set_dig_dacr(self, db: float) -> bool:
        cur = self.read(REG_DIG_DAC) or 0
        return self.write(REG_DIG_DAC, (cur & ~0xFF00) | (dig_encode(db) << 8))

    # ---- EQ ----

    def get_eq_en(self, channel: str) -> Optional[bool]:
        """Read EQ enable for channel ('adc' or 'dac')."""
        addr = REG_EQ_ADC_EN if channel == 'adc' else REG_EQ_DAC_EN
        v = self.read(addr)
        return bool(v & 1) if v is not None else None

    def set_eq_en(self, channel: str, en: bool) -> bool:
        """Enable or disable EQ for channel ('adc' or 'dac')."""
        addr = REG_EQ_ADC_EN if channel == 'adc' else REG_EQ_DAC_EN
        cur = self.read(addr) or 0
        cur = (cur | 1) if en else (cur & ~1)
        return self.write(addr, cur)

    def get_eq_band(self, channel: str, band: int) -> Optional[dict]:
        """Read one EQ band: {freq, gain, ftype, q}."""
        if not (0 <= band < REG_EQ_BANDS):
            raise ValueError(f"Band {band} out of range 0..{REG_EQ_BANDS-1}")
        base = REG_EQ_BASE if channel == 'adc' else REG_EQ_DAC_BASE
        a_addr = base + band * REG_EQ_STRIDE
        b_addr = a_addr + 1
        va = self.read(a_addr)
        vb = self.read(b_addr)
        if va is None or vb is None:
            return None
        freq, gain = _eq_decode_a(va)
        ftype, q = _eq_decode_b(vb)
        return {'freq': freq, 'gain': gain, 'ftype': ftype, 'q': q}

    def set_eq_band(self, channel: str, band: int, *,
                    freq=None, gain=None, ftype=None, q=None) -> bool:
        """Write one EQ band. None = keep current value."""
        cur = self.get_eq_band(channel, band)
        if cur is None:
            return False
        p = {**cur,
             **{k: v for k, v in [('freq', freq), ('gain', gain),
                                    ('ftype', ftype), ('q', q)]
                if v is not None}}
        base = REG_EQ_BASE if channel == 'adc' else REG_EQ_DAC_BASE
        a_addr = base + band * REG_EQ_STRIDE
        b_addr = a_addr + 1
        ok = True
        ok &= self.write(a_addr, eq_encode_a(p['freq'], p['gain']))
        ok &= self.write(b_addr, eq_encode_b(p['ftype'], p['q']))
        return ok

    # ---- batch state ----

    def state(self) -> dict:
        """Read complete DSP state with semantic (decoded) values."""
        s = {}

        # Identification
        s['product']  = _decode_ascii({a: self.read(a) for a in range(REG_PRODUCT, REG_PRODUCT + 8)}, REG_PRODUCT, 8)
        s['vendor']   = _decode_ascii({a: self.read(a) for a in range(REG_MANUF, REG_MANUF + 8)}, REG_MANUF, 8)
        s['serial']   = _decode_ascii({a: self.read(a) for a in range(REG_SERIAL, REG_SERIAL + 7)}, REG_SERIAL, 7)
        s['version']  = _decode_ascii({a: self.read(a) for a in range(REG_VERSION, REG_VERSION + 2)}, REG_VERSION, 2)
        s['vidpid']   = self.read(REG_VIDPID)
        s['magic']    = self.read(REG_MAGIC)

        # Protocol / model
        flags = self.get_protocol_flags()
        s['protocol_flags'] = flags
        s['single_dac'] = bool(flags is not None and (flags & 0x200))

        # Volume
        pa = self.get_pga_adc()
        pd = self.get_pga_dac()
        s['pga_adc_idx'] = pa
        s['pga_adc_gain'] = PGA_ADC_GAINS[pa] if pa is not None and 0 <= pa < len(PGA_ADC_GAINS) else None
        s['pga_dac_idx'] = pd
        s['pga_dac_gain'] = PGA_DAC_GAINS[pd] if pd is not None and 0 <= pd < len(PGA_DAC_GAINS) else None
        s['dig_adc_db'] = self.get_dig_adc()
        s['dig_dacl_db'] = self.get_dig_dacl()
        s['dig_dacr_db'] = self.get_dig_dacr()

        # EQ
        s['eq_adc_en'] = self.get_eq_en('adc')
        s['eq_dac_en'] = self.get_eq_en('dac')
        s['eq_adc_bands'] = [self.get_eq_band('adc', i) for i in range(REG_EQ_BANDS)]
        s['eq_dac_bands'] = [self.get_eq_band('dac', i) for i in range(REG_EQ_BANDS)]

        # DRC
        s['ng'] = self.get_ng()
        s['lim'] = self.get_lim()

        return s

    # ---- legacy info ----

    def info(self) -> dict:
        """Read device identification (returns register-address-keyed dict for compat)."""
        r = {}
        for start, count in [(REG_VERSION, 2), (REG_MANUF, 8),
                              (REG_PRODUCT, 8), (REG_SERIAL, 7)]:
            vals = {}
            for a in range(start, min(start + count, 0x100)):
                v = self.read(a)
                if v is not None:
                    vals[a] = v
            if vals:
                r[start] = _decode_ascii(vals, start, count)

        for a in [REG_VIDPID, REG_PGA_ADC, REG_PGA_DAC, REG_MARKER, REG_FLAGS]:
            v = self.read(a)
            if v is not None:
                r[a] = v
        return r
