#!/usr/bin/env python3
"""KTMicro USB Audio Chip Configuration Tool."""

import argparse
import struct
import sys
import time

from hid_transport import HIDTransport, KT_VID, KT_PID
from KT02H20 import (KT02H20,
                       REG_VERSION, REG_MANUF, REG_PRODUCT, REG_SERIAL,
                       REG_VIDPID, REG_FLAGS, REG_EQ_ADC_EN, REG_EQ_DAC_EN,
                       REG_EQ_BASE, REG_EQ_DAC_BASE, REG_EQ_STRIDE, REG_EQ_BANDS,
                       REG_PGA_ADC, REG_PGA_DAC, REG_DIG_ADC, REG_DIG_DAC,
                       PGA_ADC_GAINS, PGA_DAC_GAINS)
from KT02H20_boot import KT02H20Boot


# ── NG encoding ────────────────────────────────────────────────

NG_FLAGS = 0x3C   # bits 0-6 in byte3 of reg 0x71 (module ID, hold time, etc)

def _ng_byte_db(dB: int) -> int:
    """Encode dB threshold as uint8:  256 + dB."""
    v = 256 + dB
    if not (0 <= v <= 255):
        sys.exit(f"Error: dB value {dB:+d} maps to byte {v}, must be in range -256..-1")
    return v

def _ng_byte_to_db(b: int) -> int:
    """Decode uint8 threshold → dB."""
    return b - 256

def ng_encode_71(en: bool, gatevol_dB: int, thhigh_dB: int, thlow_dB: int) -> int:
    """Encode register 0x71 from NG parameters."""
    b3 = NG_FLAGS | (0x80 if en else 0x00)
    return (b3 << 24) | (_ng_byte_db(gatevol_dB) << 16) | (_ng_byte_db(thhigh_dB) << 8) | _ng_byte_db(thlow_dB)

def ng_encode_72(rt_ms: int, at_ms: int) -> int:
    """Encode register 0x72: hi16=RTms, lo16=ATms."""
    return ((rt_ms & 0xFFFF) << 16) | (at_ms & 0xFFFF)

def ng_encode_73(noiseT_ms: int) -> int:
    """Encode register 0x73: hi16=noiseTms, lo16=0x000A."""
    return ((noiseT_ms & 0xFFFF) << 16) | 0x000A

def ng_decode_71(v: int):
    """Decode register 0x71 → {en, gatevol_dB, thhigh_dB, thlow_dB, flags}."""
    return {
        'en':       bool((v >> 24) & 0x80),
        'flags':    (v >> 24) & 0x7F,
        'gatevol':  _ng_byte_to_db((v >> 16) & 0xFF),
        'thhigh':   _ng_byte_to_db((v >> 8) & 0xFF),
        'thlow':    _ng_byte_to_db(v & 0xFF),
    }

def ng_decode_72(v: int):
    """Decode register 0x72 → {rt_ms, at_ms}."""
    return {'rt_ms': (v >> 16) & 0xFFFF, 'at_ms': v & 0xFFFF}

def ng_decode_73(v: int):
    """Decode register 0x73 → {noiseT_ms, hold}."""
    return {'noiseT_ms': (v >> 16) & 0xFFFF, 'hold': v & 0xFFFF}

def lim_decode_78(v: int):
    """Decode Limiter register 0x78 → {en, soft, threshold_dB}."""
    b0 = v & 0xFF
    b3 = (v >> 24) & 0xFF
    return {
        'en':        bool(b3 & 0x80),
        'soft':      bool(b3 & 0x40),
        'threshold': b0 - 256,
    }

def lim_encode_78(en: bool, soft: bool, threshold_dB: int) -> int:
    """Encode Limiter register 0x78."""
    b3 = 0
    if en:   b3 |= 0x80
    if soft: b3 |= 0x40
    return (b3 << 24) | _ng_byte_db(threshold_dB)

def lim_decode_79(v: int):
    """Decode Limiter register 0x79 → {rt_ms, at_ms}."""
    return {'rt_ms': (v >> 16) & 0xFFFF, 'at_ms': v & 0xFFFF}

def lim_encode_79(rt_ms: int, at_ms: int) -> int:
    """Encode Limiter register 0x79."""
    return ((rt_ms & 0xFFFF) << 16) | (at_ms & 0xFFFF)


# ── EQ encoding ────────────────────────────────────────────────
# A_reg = [freq_Hz:16][gain_x10:16 signed]   B_reg = [type:3][res:13][Q_x1000:16]

def eq_encode_a(freq_hz: int, gain_db: float) -> int:
    g10 = int(round(gain_db * 10)) & 0xFFFF
    return ((freq_hz & 0xFFFF) << 16) | g10

def eq_encode_b(ftype: int, q: float) -> int:
    q1000 = int(round(q * 1000)) & 0xFFFF
    return ((ftype & 0x7) << 16) | q1000

# ── Volume encoding ────────────────────────────────────────────

def dig_encode(db: float) -> int:
    """Digital gain dB -> register byte (signed, 0.5 dB steps)."""
    v = int(round(db * 2))
    if not (-128 <= v <= 255):
        sys.exit(f"Error: digital gain {db} dB out of range (-64..+127.5 dB)")
    return v & 0xFF

def dig_decode(b: int) -> float:
    """Register byte -> dB (app uses movsx: byte is SIGNED, x0.5)."""
    if b >= 0x80:
        b -= 0x100
    return b / 2.0

def _single_dac(flags) -> bool:
    """Register 0x01 bit9 (0x0200) selects the single-DAC UI/model."""
    return bool(flags is not None and (flags & 0x200))


# ── Display helpers ────────────────────────────────────────────

_FILTER_TYPES = {0: 'P', 1: 'LPF', 2: 'HPF', 3: 'LS', 4: 'HS'}

_DRC_KNOWN = {0x71, 0x72, 0x73, 0x78, 0x79}

_DRC_NAMES = {
    0x71: 'NoiseGate:  thresholds',
    0x72: '            RT/AT',
    0x73: '            noiseT',
    0x78: 'Limiter:     EN+SOFT+pt',
    0x79: '            RT/AT',
}

def _decode_drc(a: int, v: int) -> str:
    b = struct.pack('<I', v)
    alnum = sum(1 for c in b if (48 <= c <= 57) or (65 <= c <= 90) or (97 <= c <= 122))
    if all(32 <= c < 127 for c in b) and alnum >= 1:
        return f"'{''.join(chr(c) for c in b)}'"

    if a == 0x71:
        d = ng_decode_71(v)
        return (f"EN={'ON' if d['en'] else 'OFF'}  "
                f"GateVol={d['gatevol']:+d} dB  "
                f"THhigh={d['thhigh']:+d} dB  "
                f"THlow={d['thlow']:+d} dB  "
                f"flags=0x{d['flags']:02X}")
    if a == 0x72:
        d = ng_decode_72(v)
        return f"RT={d['rt_ms']} ms  AT={d['at_ms']} ms"
    if a == 0x73:
        d = ng_decode_73(v)
        return f"noiseT={d['noiseT_ms']} ms  hold=0x{d['hold']:04X}"

    if a == 0x78:
        d = lim_decode_78(v)
        return (f"EN={'ON' if d['en'] else 'OFF'}  "
                f"SOFT={'ON' if d['soft'] else 'OFF'}  "
                f"Point={d['threshold']:+d} dB")
    if a == 0x79:
        d = lim_decode_79(v)
        return f"RT={d['rt_ms']} ms  AT={d['at_ms']} ms"

    return f"raw=0x{v:08X}"


def _decode_q(val: int) -> float:
    return (val & 0xFFFF) / 1000.0 if val else 0.0


def _decode_filt(va: int, vb: int) -> tuple:
    """Decode 32-bit value register A and B into (type, freq_hz, gain_db, Q)."""
    hi16   = (va >> 16) & 0xFFFF
    lo16_s = va & 0xFFFF
    if lo16_s >= 0x8000:
        lo16_s -= 0x10000
    gain_db = lo16_s / 10.0

    q     = _decode_q(vb)
    ftype = _FILTER_TYPES.get((vb >> 16) & 0x7, f"cfg=0x{(vb>>16)&0x7:01X}")
    freq  = f"{hi16} Hz" if hi16 else "—"

    return ftype, freq, gain_db, q


def _print_state(s: dict):
    """Pretty-print DSP state with decoded parameters."""
    print(f"Device:  {s.get('vendor','?')} (0x40) {s.get('product','?')} (0x48)")
    print(f"Version: {s.get('version','?')} (0x04)")
    print(f"Serial:  {s.get('serial','?')} (0x50)")
    vp = s.get('vidpid')
    if vp: print(f"VID:PID:  {vp&0xFFFF:04X}:{vp>>16:04X} (0x5B)")
    magic = s.get('magic')
    if magic: print(f"Magic:    0x{magic:08X} (0xE1)")
    proto = s.get('chip_flags')
    if proto is not None:
        model = 'single-DAC' if _single_dac(proto) else 'stereo-DAC'
        print(f"Protocol: 0x{proto:08X} (0x01)  [{model}]")

    # Volume
    pa, pd = s.get('pga_adc'), s.get('pga_dac')
    da, dd = s.get('dig_adc'), s.get('dig_dac')
    print(f"\n── Volume ──")
    if pa is not None:
        g = PGA_ADC_GAINS[pa] if 0 <= pa < len(PGA_ADC_GAINS) else f'idx {pa}'
        print(f"  A_ADC (0x3A): {g}")
    if pd is not None:
        g = PGA_DAC_GAINS[pd] if 0 <= pd < len(PGA_DAC_GAINS) else f'idx {pd}'
        print(f"  A_DAC (0x3B): {g}")
    if da is not None:
        print(f"  DIG_ADC (0x65): {dig_decode(da & 0xFF):+.1f} dB")
    if dd is not None:
        if _single_dac(s.get('chip_flags')):
            print(f"  DIG_DAC (0x66 b0): {dig_decode(dd & 0xFF):+.1f} dB   (single-DAC model)")
        else:
            print(f"  DIG_DACL (0x66 b0): {dig_decode(dd & 0xFF):+.1f} dB   "
                  f"DIG_DACR (0x66 b1): {dig_decode((dd >> 8) & 0xFF):+.1f} dB")

    def _print_bands(title, en_addr, en_val, bands):
        en_s = 'ON' if (en_val or 0) & 1 else 'OFF'
        print(f"\n── {title} (enable={en_s} @ 0x{en_addr:02X}) ──")
        print(f"  {'#':>3}  {'Type':>4}  {'Freq':>7}  {'Gain':>8}  {'Q':>7}  {'Reg A':>14}  {'Reg B':>14}")
        print(f"  {'-'*3}  {'-'*4}  {'-'*7}  {'-'*8}  {'-'*7}  {'-'*14}  {'-'*14}")
        for b in bands:
            va = b['val_a'] or 0
            vb = b['val_b'] or 0
            t, f, g, q = _decode_filt(va, vb)
            gs = f"{g:+.1f} dB" if g else ''
            print(f"  {b['index']:3d}  {t:>4}  {f:>7}  {gs:>8}  {q:6.3f}  {b['addr_a']}:0x{va:08X}  {b['addr_b']}:0x{vb:08X}")

    if s.get('eq_bands'):
        _print_bands('ADC EQ Filters (5 bands)', REG_EQ_ADC_EN, s.get('eq_adc_en'), s['eq_bands'])
    if s.get('eq_dac_bands'):
        _print_bands('DAC EQ Filters (5 bands)', REG_EQ_DAC_EN, s.get('eq_dac_en'), s['eq_dac_bands'])

    # DRC/AGC
    drc = s.get('drc', {})
    if drc:
        print(f"\n── DRC / AGC ──")
        for a in sorted(drc.keys()):
            v = drc[a]
            if v is None: continue
            if a not in _DRC_KNOWN: continue
            name = _DRC_NAMES.get(a, '')
            info = _decode_drc(a, v)
            print(f"  0x{a:02X}: 0x{v:08X}  {name:24s}  {info}")


# ── Commands ───────────────────────────────────────────────────

_DUMP_MARKS = {
    0x00: '  ← VERSION',
    0x08: '  ← DATE',
    0x18: '  ← ADC EQ ENABLE',
    0x1A: '  ← ADC EQ BANDS',
    0x24: '  ← DAC EQ ENABLE',
    0x26: '  ← DAC EQ BANDS',
    0x3A: '  ← A_ADC PGA',
    0x3B: '  ← A_DAC PGA',
    0x40: '  ← USB MANUFACTURER',
    0x48: '  ← USB PRODUCT',
    0x50: '  ← USB SERIAL',
    0x5B: '  ← USB VID:PID',
    0x65: '  ← DIG_ADC',
    0x66: '  ← DIG_DAC L/R',
    0x71: '  ← NOISEGATE',
    0x78: '  ← LIMITER',
    0xE1: '  ← MAGIC',
}


def cmd_list(_):
    devs = HIDTransport.list_devices()
    kt = [d for d in devs if d.get('vendor_id') == KT_VID]
    print(f"KTMicro devices: {len(kt)}")
    for d in kt:
        print(f"  {d.get('product_string', '?')}  "
              f"VID=0x{d['vendor_id']:04X} PID=0x{d['product_id']:04X}  "
              f"intf={d.get('interface_number', '?')}  "
              f"{d.get('path', b'')}")
    if not kt:
        print("  None. All HID devices:")
        for d in devs:
            print(f"  VID=0x{d.get('vendor_id',0):04X} PID=0x{d.get('product_id',0):04X}  "
                  f"{d.get('product_string','?')}")


def cmd_info(_):
    d = KT02H20()
    if not d.open():
        print("Device not found"); return
    info = d.info()
    d.close()

    for label, key in [("Version",  REG_VERSION),
                        ("Vendor",   REG_MANUF),
                        ("Product",  REG_PRODUCT),
                        ("Serial",   REG_SERIAL)]:
        s = info.get(key, '')
        if s: print(f"  {label}: {s} (0x{key:02X})")

    vp = info.get(REG_VIDPID)
    if vp: print(f"  VID:PID: {vp&0xFFFF:04X}:{vp>>16:04X} (0x{REG_VIDPID:02X})")

    pa = info.get(REG_PGA_ADC)
    if pa is not None:
        g = PGA_ADC_GAINS[pa] if 0 <= pa < len(PGA_ADC_GAINS) else f'idx {pa}'
        print(f"  A_ADC: {g} (0x{REG_PGA_ADC:02X})")
    pd = info.get(REG_PGA_DAC)
    if pd is not None:
        g = PGA_DAC_GAINS[pd] if 0 <= pd < len(PGA_DAC_GAINS) else f'idx {pd}'
        print(f"  A_DAC: {g} (0x{REG_PGA_DAC:02X})")


def cmd_state(_):
    d = KT02H20()
    if not d.open():
        print("Device not found"); return
    s = d.state()
    d.close()
    _print_state(s)


def cmd_dump(_):
    d = KT02H20()
    if not d.open():
        print("Device not found"); return
    for addr in range(0x100):
        val = d.read(addr)
        if val is None:
            print(f"  [0x{addr:02X}] TIMEOUT"); continue
        asc = ''.join(chr(b) if 32 <= b < 127 else '.'
                      for b in struct.pack('<I', val))
        mark = _DUMP_MARKS.get(addr, '')
        print(f"  [0x{addr:02X}] 0x{val:08X} '{asc}'{mark}")
    d.close()


def cmd_read(args):
    d = KT02H20()
    if not d.open():
        print("Device not found"); return
    addr = _parse_int(args.addr)
    val = d.read(addr)
    d.close()
    if val is not None:
        asc = ''.join(chr(b) if 32 <= b < 127 else '.'
                      for b in struct.pack('<I', val))
        print(f"reg[0x{addr:04X}] = 0x{val:08X} ({val})  '{asc}'")
    else:
        print(f"reg[0x{addr:04X}] = (no response)")


def cmd_write(args):
    d = KT02H20()
    if not d.open():
        print("Device not found"); return
    addr = _parse_int(args.addr)
    val  = _parse_int(args.val)
    ok = d.write(addr, val)
    print(f"Write 0x{addr:04X} = 0x{val:08X} → {'ACK' if ok else 'FAILED'}")
    if ok:
        time.sleep(0.01)
        rv = d.read(addr)
        if rv is not None:
            ok2 = '✓' if rv == val else f'✗ (read 0x{rv:08X})'
            print(f"  Read-back: 0x{rv:08X} {ok2}")
    d.close()


def cmd_ng(args):
    """Read + optionally write NoiseGate parameters."""
    d = KT02H20()
    if not d.open():
        print("Device not found"); return

    if args.set_param:
        # Read current, modify one param, write back
        v71 = d.read(0x71)
        v72 = d.read(0x72)
        v73 = d.read(0x73)
        if v71 is None or v72 is None or v73 is None:
            print("Failed to read NG registers"); d.close(); return

        ng71 = ng_decode_71(v71)
        ng72 = ng_decode_72(v72)
        ng73 = ng_decode_73(v73)

        p = args.set_param
        new_val = _parse_int(args.value) if p in ('at', 'rt', 'noiset') else int(args.value)

        if p == 'en':
            ng71['en'] = new_val != 0
        elif p in ('gatevol', 'thhigh', 'thlow'):
            ng71[p] = new_val
        elif p == 'at':
            ng72['at_ms'] = new_val
        elif p == 'rt':
            ng72['rt_ms'] = new_val
        elif p == 'noiset':
            ng73['noiseT_ms'] = new_val
        else:
            print(f"Unknown parameter: {p}"); d.close(); return

        n71 = ng_encode_71(ng71['en'], ng71['gatevol'], ng71['thhigh'], ng71['thlow'])
        n72 = ng_encode_72(ng72['rt_ms'], ng72['at_ms'])
        n73 = ng_encode_73(ng73['noiseT_ms'])

        for addr, val in [(0x71, n71), (0x72, n72), (0x73, n73)]:
            d.write(addr, val)

    # Show current NG state
    v71 = d.read(0x71)
    v72 = d.read(0x72)
    v73 = d.read(0x73)
    d.close()

    if v71 is None or v72 is None or v73 is None:
        print("Failed to read NG registers"); return

    print("── NoiseGate ──")
    d71 = ng_decode_71(v71)
    print(f"  0x71: 0x{v71:08X}  EN={'ON' if d71['en'] else 'OFF'}  "
          f"GateVol={d71['gatevol']:+d} dB  THhigh={d71['thhigh']:+d} dB  "
          f"THlow={d71['thlow']:+d} dB  flags=0x{d71['flags']:02X}")
    d72 = ng_decode_72(v72)
    print(f"  0x72: 0x{v72:08X}  RT={d72['rt_ms']} ms  AT={d72['at_ms']} ms")
    d73 = ng_decode_73(v73)
    print(f"  0x73: 0x{v73:08X}  noiseT={d73['noiseT_ms']} ms  hold=0x{d73['hold']:04X}")


def cmd_lim(args):
    """Read + optionally write Limiter parameters."""
    d = KT02H20()
    if not d.open():
        print("Device not found"); return

    if args.set_param:
        v78 = d.read(0x78)
        v79 = d.read(0x79)
        if v78 is None or v79 is None:
            print("Failed to read Limiter registers"); d.close(); return

        lim78 = lim_decode_78(v78)
        lim79 = lim_decode_79(v79)

        p = args.set_param
        new_val = _parse_int(args.value) if p in ('at', 'rt') else int(args.value)

        if p == 'en':
            lim78['en'] = new_val != 0
        elif p == 'soft':
            lim78['soft'] = new_val != 0
        elif p == 'point':
            lim78['threshold'] = new_val
        elif p == 'at':
            lim79['at_ms'] = new_val
        elif p == 'rt':
            lim79['rt_ms'] = new_val
        else:
            print(f"Unknown parameter: {p}"); d.close(); return

        n78 = lim_encode_78(lim78['en'], lim78['soft'], lim78['threshold'])
        n79 = lim_encode_79(lim79['rt_ms'], lim79['at_ms'])

        for addr, val in [(0x78, n78), (0x79, n79)]:
            d.write(addr, val)

    v78 = d.read(0x78)
    v79 = d.read(0x79)
    d.close()

    if v78 is None or v79 is None:
        print("Failed to read Limiter registers"); return

    print("── Limiter ──")
    d78 = lim_decode_78(v78)
    print(f"  0x78: 0x{v78:08X}  EN={'ON' if d78['en'] else 'OFF'}  "
          f"SOFT={'ON' if d78['soft'] else 'OFF'}  "
          f"Point={d78['threshold']:+d} dB")
    d79 = lim_decode_79(v79)
    print(f"  0x79: 0x{v79:08X}  RT={d79['rt_ms']} ms  AT={d79['at_ms']} ms")


def cmd_vol(args):
    """Read + optionally write volume (PGA + digital gains)."""
    d = KT02H20()
    if not d.open():
        print("Device not found"); return

    if args.set_param:
        p = args.set_param
        if p in ('pga_adc', 'pga_dac'):
            table = PGA_ADC_GAINS if p == 'pga_adc' else PGA_DAC_GAINS
            addr = REG_PGA_ADC if p == 'pga_adc' else REG_PGA_DAC
            idx = _pga_resolve(table, args.value)
            if idx is None:
                print(f"Invalid PGA value '{args.value}'. Table: " +
                      ", ".join(f"{i}={g}" for i, g in enumerate(table)))
                d.close(); return
            d.write(addr, idx)
        else:
            db = float(args.value)
            if p == 'dig_adc':
                cur = d.read(REG_DIG_ADC) or 0
                d.write(REG_DIG_ADC, (cur & ~0xFF) | dig_encode(db))
            elif p == 'dig_dac':
                # single-DAC models: gain lives in byte0 of 0x66
                cur = d.read(REG_DIG_DAC) or 0
                d.write(REG_DIG_DAC, (cur & ~0xFF) | dig_encode(db))
            elif p in ('dig_dacl', 'dig_dacr'):
                cur = d.read(REG_DIG_DAC) or 0
                if p == 'dig_dacl':
                    cur = (cur & ~0xFF) | dig_encode(db)
                else:
                    cur = (cur & ~0xFF00) | (dig_encode(db) << 8)
                d.write(REG_DIG_DAC, cur)

    flags = d.read(REG_FLAGS)
    pa = d.read(REG_PGA_ADC); pd = d.read(REG_PGA_DAC)
    da = d.read(REG_DIG_ADC); dd = d.read(REG_DIG_DAC)
    d.close()

    print("── Volume ──")
    if pa is not None:
        g = PGA_ADC_GAINS[pa] if 0 <= pa < len(PGA_ADC_GAINS) else f'idx {pa}'
        print(f"  A_ADC  (0x3A): idx {pa} = {g}")
    if pd is not None:
        g = PGA_DAC_GAINS[pd] if 0 <= pd < len(PGA_DAC_GAINS) else f'idx {pd}'
        print(f"  A_DAC  (0x3B): idx {pd} = {g}")
    if da is not None:
        print(f"  DIG_ADC  (0x65): {dig_decode(da & 0xFF):+.1f} dB")
    if dd is not None:
        if _single_dac(flags):
            print(f"  DIG_DAC  (0x66 b0): {dig_decode(dd & 0xFF):+.1f} dB   (single-DAC model)")
        else:
            print(f"  DIG_DACL (0x66 b0): {dig_decode(dd & 0xFF):+.1f} dB   "
                  f"DIG_DACR (0x66 b1): {dig_decode((dd >> 8) & 0xFF):+.1f} dB")


def cmd_eq(args):
    """Read + optionally write EQ bands."""
    d = KT02H20()
    if not d.open():
        print("Device not found"); return

    ch = args.channel
    base = REG_EQ_BASE if ch == 'adc' else REG_EQ_DAC_BASE
    en_addr = REG_EQ_ADC_EN if ch == 'adc' else REG_EQ_DAC_EN

    if args.enable or args.disable:
        cur = d.read(en_addr) or 0
        cur = (cur | 1) if args.enable else (cur & ~1)
        d.write(en_addr, cur)

    if args.set_band is not None:
        n = args.set_band
        if not (0 <= n < REG_EQ_BANDS):
            print(f"Band {n} out of range 0..{REG_EQ_BANDS-1}"); d.close(); return
        a_addr = base + n * REG_EQ_STRIDE
        b_addr = a_addr + 1
        va = d.read(a_addr) or 0
        vb = d.read(b_addr) or 0
        t0, _, g0, q0 = _decode_filt(va, vb)
        freq = args.freq if args.freq is not None else (va >> 16) & 0xFFFF
        gain = args.gain if args.gain is not None else g0
        ftype = _TYPE_NAMES[args.ftype] if args.ftype else \
            next((k for k, v in _FILTER_TYPES.items() if v == t0), 0)
        q = args.q if args.q is not None else q0
        d.write(a_addr, eq_encode_a(freq, gain))
        d.write(b_addr, eq_encode_b(ftype, q))

    # show
    en = d.read(en_addr)
    print(f"── {ch.upper()} EQ ──  enable={'ON' if (en or 0) & 1 else 'OFF'} (0x{en_addr:02X})")
    print(f"  {'#':>3}  {'Type':>4}  {'Freq':>8}  {'Gain':>8}  {'Q':>7}  {'Regs':>12}")
    for i in range(REG_EQ_BANDS):
        a_addr = base + i * REG_EQ_STRIDE
        b_addr = a_addr + 1
        va = d.read(a_addr) or 0
        vb = d.read(b_addr) or 0
        t, f, g, q = _decode_filt(va, vb)
        print(f"  {i:3d}  {t:>4}  {f:>8}  {g:+7.1f} dB  {q:6.3f}  0x{a_addr:02X}/0x{b_addr:02X}")
    d.close()


_TYPE_NAMES = {'P': 0, 'LPF': 1, 'HPF': 2, 'LS': 3, 'HS': 4}


def _pga_resolve(table, s: str):
    """Resolve a PGA argument to a comboBox index: dB value or raw index."""
    s = s.strip().lower()
    if s == 'mute':
        return 0 if 'mute' in table else None
    try:
        db = float(s)
        # exact dB match -> index
        for i, g in enumerate(table):
            if g == 'mute':
                continue
            if float(g.replace(' dB', '')) == db:
                return i
        # else treat as raw index
        idx = int(s)
        return idx if 0 <= idx < len(table) else None
    except ValueError:
        return None


def cmd_flash(args):
    d = KT02H20Boot()
    if not d.open():
        print("Device not found (bootloader mode?)"); return
    d.flash(args.firmware, args.chip_type)
    d.close()


def _parse_int(s: str) -> int:
    return int(s, 16) if s.startswith('0x') else int(s)


def main():
    p = argparse.ArgumentParser(description='KTMicro USB Audio Chip Tool')
    sp = p.add_subparsers(dest='cmd')

    sp.add_parser('list',  help='List connected devices')
    sp.add_parser('info',  help='Read chip identification')
    sp.add_parser('state', help='Read full DSP state')
    sp.add_parser('dump',  help='Dump all 256 registers')

    r = sp.add_parser('read', help='Read one register')
    r.add_argument('addr', help='Register address (hex)')

    w = sp.add_parser('write', help='Write one register')
    w.add_argument('addr', help='Register address (hex)')
    w.add_argument('val',  help='Value (hex or dec)')

    ng = sp.add_parser('ng', help='Read / set NoiseGate parameters',
                       description='Read or modify NoiseGate settings')
    ng.add_argument('--set', dest='set_param', metavar='PARAM',
                    choices=['en', 'gatevol', 'thhigh', 'thlow', 'at', 'rt', 'noiset'],
                    help='Parameter to set')
    ng.add_argument('value', nargs='?', help='New value (1/0 for EN, dB for thresholds, ms for times)')

    lim = sp.add_parser('lim', help='Read / set Limiter parameters',
                        description='Read or modify Limiter settings')
    lim.add_argument('--set', dest='set_param', metavar='PARAM',
                     choices=['en', 'soft', 'point', 'at', 'rt'],
                     help='Parameter to set')
    lim.add_argument('value', nargs='?', help='New value (1/0 for EN/SOFT, dB for point, ms for times)')

    vol = sp.add_parser('vol', help='Read / set volume (PGA + digital gains)',
                        description='Read or modify volume: analog PGA indices and digital gains')
    vol.add_argument('--set', dest='set_param', metavar='PARAM',
                     choices=['pga_adc', 'pga_dac', 'dig_adc', 'dig_dac', 'dig_dacl', 'dig_dacr'],
                     help='Parameter to set (dig_dac = single-DAC models, dig_dacl/dacr = stereo models)')
    vol.add_argument('value', nargs='?',
                     help='PGA: dB value or index; digital: gain in dB (0.5 dB steps)')

    eq = sp.add_parser('eq', help='Read / set EQ bands',
                       description='Read or modify 5-band PEQ per channel')
    eq.add_argument('channel', choices=['adc', 'dac'], help='EQ channel')
    eq.add_argument('--set', dest='set_band', type=int, metavar='BAND',
                    help='Band index 0-4 to modify')
    eq.add_argument('--type', dest='ftype', choices=['P', 'LPF', 'HPF', 'LS', 'HS'],
                    help='Filter type')
    eq.add_argument('--freq', type=int, help='Frequency in Hz')
    eq.add_argument('--gain', type=float, help='Gain in dB')
    eq.add_argument('--q', type=float, help='Q factor')
    eq.add_argument('--enable', action='store_true', help='Enable this EQ channel')
    eq.add_argument('--disable', action='store_true', help='Disable this EQ channel')

    f = sp.add_parser('flash', help='Flash firmware (bootloader mode)')
    f.add_argument('firmware', help='Firmware .bin path')
    f.add_argument('--chip-type', type=int, default=0x10)

    args = p.parse_args()
    if   args.cmd == 'list':   cmd_list(args)
    elif args.cmd == 'info':   cmd_info(args)
    elif args.cmd == 'state':  cmd_state(args)
    elif args.cmd == 'dump':   cmd_dump(args)
    elif args.cmd == 'read':   cmd_read(args)
    elif args.cmd == 'write':  cmd_write(args)
    elif args.cmd == 'ng':     cmd_ng(args)
    elif args.cmd == 'lim':    cmd_lim(args)
    elif args.cmd == 'vol':    cmd_vol(args)
    elif args.cmd == 'eq':     cmd_eq(args)
    elif args.cmd == 'flash':  cmd_flash(args)
    else: p.print_help()


if __name__ == '__main__':
    main()
