#!/usr/bin/env python3
"""KTMicro USB Audio Chip Configuration Tool."""

import argparse
import os
import struct
import sys
import time

from typing import Optional

from hid_transport import HIDTransport, KT_VID, KT_PID
from kt02h20 import (KT02H20,
                     REG_FLAGS, REG_VERSION, REG_MANUF, REG_PRODUCT, REG_SERIAL,
                     REG_VIDPID, REG_EQ_ADC_EN, REG_EQ_DAC_EN,
                     REG_EQ_BASE, REG_EQ_DAC_BASE, REG_EQ_STRIDE, REG_EQ_BANDS,
                     REG_PGA_ADC, REG_PGA_DAC, REG_DIG_ADC, REG_DIG_DAC,
                     REG_NG_BASE, REG_LIM_BASE,
                     PGA_ADC_GAINS, PGA_DAC_GAINS,
                     FILTER_TYPES, TYPE_NAMES,
                     dig_encode, dig_decode,
                     ng_decode_71, ng_decode_72, ng_decode_73,
                     lim_decode_78, lim_decode_79)
from kt02h20_boot import KT02H20Boot


# ── Verbose logging ────────────────────────────────────────────

_VERBOSE = False

def _log(msg: str):
    if _VERBOSE:
        print(f"[dbg] {msg}")


# ── Display helpers ────────────────────────────────────────────

_DUMP_MARKS = {
    0x00: '  ← MARKER',
    0x01: '  ← PROTOCOL / FLAGS',
    0x04: '  ← VERSION STRING',
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
    REG_NG_BASE:     '  ← NOISEGATE',
    REG_LIM_BASE:    '  ← LIMITER',
    0xE1: '  ← MAGIC',
}


def _print_state(s: dict):
    """Pretty-print DSP state (semantic values from dev.state())."""
    print(f"Device:  {s.get('vendor','?')} (0x40) {s.get('product','?')} (0x48)")
    print(f"Version: {s.get('version','?')} (0x04)")
    print(f"Serial:  {s.get('serial','?')} (0x50)")
    vp = s.get('vidpid')
    if vp: print(f"VID:PID:  {vp&0xFFFF:04X}:{vp>>16:04X} (0x5B)")
    magic = s.get('magic')
    if magic: print(f"Magic:    0x{magic:08X} (0xE1)")
    proto = s.get('protocol_flags')
    if proto is not None:
        model = 'single-DAC' if s.get('single_dac') else 'stereo-DAC'
        print(f"Protocol: 0x{proto:08X} (0x01)  [{model}]")

    # Volume
    print(f"\n── Volume ──")
    if s.get('pga_adc_gain') is not None:
        print(f"  A_ADC (0x3A): {s['pga_adc_gain']}")
    if s.get('pga_dac_gain') is not None:
        print(f"  A_DAC (0x3B): {s['pga_dac_gain']}")
    if s.get('dig_adc_db') is not None:
        print(f"  DIG_ADC (0x65): {s['dig_adc_db']:+.1f} dB")
    if s.get('dig_dacl_db') is not None:
        if s.get('single_dac'):
            print(f"  DIG_DAC (0x66 b0): {s['dig_dacl_db']:+.1f} dB   (single-DAC model)")
        else:
            dacr = s.get('dig_dacr_db')
            dacr_s = f"{dacr:+.1f} dB" if dacr is not None else "?"
            print(f"  DIG_DACL (0x66 b0): {s['dig_dacl_db']:+.1f} dB   "
                  f"DIG_DACR (0x66 b1): {dacr_s}")

    # EQ
    for ch, en_key, bands_key in [('ADC', 'eq_adc_en', 'eq_adc_bands'),
                                    ('DAC', 'eq_dac_en', 'eq_dac_bands')]:
        bands = s.get(bands_key)
        if bands is None:
            continue
        en = s.get(en_key)
        en_s = 'ON' if en else 'OFF'
        en_addr = REG_EQ_ADC_EN if ch == 'ADC' else REG_EQ_DAC_EN
        print(f"\n── {ch} EQ Filters (5 bands) (enable={en_s} @ 0x{en_addr:02X}) ──")
        print(f"  {'#':>3}  {'Type':>4}  {'Freq':>7}  {'Gain':>8}  {'Q':>7}")
        print(f"  {'-'*3}  {'-'*4}  {'-'*7}  {'-'*8}  {'-'*7}")
        for i, b in enumerate(bands):
            if b is None:
                print(f"  {i:3d}  (timeout)")
                continue
            t = FILTER_TYPES.get(b['ftype'], f"cfg={b['ftype']}")
            f = f"{b['freq']} Hz" if b['freq'] else "—"
            g = f"{b['gain']:+.1f} dB" if b['gain'] else ''
            print(f"  {i:3d}  {t:>4}  {f:>7}  {g:>8}  {b['q']:6.3f}")

    # DRC
    ng = s.get('ng')
    lim = s.get('lim')
    if ng or lim:
        print(f"\n── DRC / AGC ──")
        if ng:
            print(f"  NoiseGate:  EN={'ON' if ng['en'] else 'OFF'}  "
                  f"GateVol={ng['gatevol']:+d} dB  THhigh={ng['thhigh']:+d} dB  "
                  f"THlow={ng['thlow']:+d} dB  flags=0x{ng['flags']:02X}")
            print(f"              RT={ng['rt']} ms  AT={ng['at']} ms  noiseT={ng['noiset']} ms")
        if lim:
            print(f"  Limiter:    EN={'ON' if lim['en'] else 'OFF'}  "
                  f"SOFT={'ON' if lim['soft'] else 'OFF'}  Point={lim['point']:+d} dB")
            print(f"              RT={lim['rt']} ms  AT={lim['at']} ms")


def _print_eq_channel(dev, ch: str):
    """Read and display EQ bands for one channel."""
    en = dev.get_eq_en(ch)
    en_addr = REG_EQ_ADC_EN if ch == 'adc' else REG_EQ_DAC_EN
    print(f"── {ch.upper()} EQ ──  enable={'ON' if en else 'OFF'} (0x{en_addr:02X})")
    print(f"  {'#':>3}  {'Type':>4}  {'Freq':>8}  {'Gain':>8}  {'Q':>7}  {'Regs':>12}")
    base = REG_EQ_BASE if ch == 'adc' else REG_EQ_DAC_BASE
    for i in range(REG_EQ_BANDS):
        b = dev.get_eq_band(ch, i)
        a_addr = base + i * REG_EQ_STRIDE
        b_addr = a_addr + 1
        if b is None:
            print(f"  {i:3d}  (timeout)")
            continue
        t = FILTER_TYPES.get(b['ftype'], f"cfg={b['ftype']}")
        f = f"{b['freq']} Hz" if b['freq'] else "—"
        print(f"  {i:3d}  {t:>4}  {f:>8}  {b['gain']:+7.1f} dB  {b['q']:6.3f}  0x{a_addr:02X}/0x{b_addr:02X}")


# ── Input parsing helpers ──────────────────────────────────────

def _parse_int(s: str) -> int:
    return int(s, 16) if s.startswith(('0x', '0X')) else int(s)


def _pga_resolve(table, s: str) -> int:
    """Resolve a PGA argument to a comboBox index: dB value or raw index.

    Raises ValueError on invalid input.
    """
    s = s.strip().lower()
    if s == 'mute':
        if 'mute' in table:
            return 0
        raise ValueError("This PGA table has no 'mute' entry")
    # Try dB match first
    try:
        db = float(s)
    except ValueError:
        db = None
    if db is not None:
        for i, g in enumerate(table):
            if g == 'mute':
                continue
            if float(g.replace(' dB', '')) == db:
                return i
    # Fall through to raw index
    try:
        idx = int(s)
    except ValueError:
        raise ValueError(f"'{s}' is neither a dB value nor a valid index")
    if not (0 <= idx < len(table)):
        raise ValueError(f"Index {idx} out of range 0..{len(table)-1}. "
                         f"Valid: {', '.join(f'{i}={g}' for i, g in enumerate(table))}")
    return idx


# ── Commands ───────────────────────────────────────────────────

def cmd_list(_):
    devs = HIDTransport.list_devices()
    kt = [d for d in devs if d.get('vendor_id') == KT_VID]
    print(f"KTMicro devices: {len(kt)}")
    for d in kt:
        path = d.get('path', b'')
        if isinstance(path, bytes):
            path = path.decode('utf-8', errors='replace')
        print(f"  {d.get('product_string', '?')}  "
              f"VID=0x{d['vendor_id']:04X} PID=0x{d['product_id']:04X}  "
              f"intf={d.get('interface_number', '?')}  "
              f"{path}")
    if not kt:
        print("  None. All HID devices:")
        for d in devs:
            print(f"  VID=0x{d.get('vendor_id',0):04X} PID=0x{d.get('product_id',0):04X}  "
                  f"{d.get('product_string','?')}")


def cmd_info(_):
    try:
        with KT02H20() as dev:
            info = dev.info()
            flags = dev.get_protocol_flags()
            single = dev.is_single_dac()
            pa = dev.get_pga_adc()
            pd = dev.get_pga_dac()
    except ConnectionError:
        print("Device not found"); return

    for label, key in [("Version",  REG_VERSION),
                        ("Vendor",   REG_MANUF),
                        ("Product",  REG_PRODUCT),
                        ("Serial",   REG_SERIAL)]:
        s = info.get(key, '')
        if s: print(f"  {label}: {s} (0x{key:02X})")

    vp = info.get(REG_VIDPID)
    if vp: print(f"  VID:PID: {vp&0xFFFF:04X}:{vp>>16:04X} (0x{REG_VIDPID:02X})")

    if flags is not None:
        model = 'single-DAC' if single else 'stereo-DAC'
        print(f"  Protocol: 0x{flags:08X} (0x{REG_FLAGS:02X})  [{model}]")

    if pa is not None:
        g = PGA_ADC_GAINS[pa] if 0 <= pa < len(PGA_ADC_GAINS) else f'idx {pa}'
        print(f"  A_ADC: {g} (0x{REG_PGA_ADC:02X})")
    if pd is not None:
        g = PGA_DAC_GAINS[pd] if 0 <= pd < len(PGA_DAC_GAINS) else f'idx {pd}'
        print(f"  A_DAC: {g} (0x{REG_PGA_DAC:02X})")


def cmd_state(_):
    try:
        with KT02H20() as dev:
            s = dev.state()
    except ConnectionError:
        print("Device not found"); return
    _print_state(s)


def cmd_dump(_):
    try:
        with KT02H20() as dev:
            regs = dev.dump()
    except ConnectionError:
        print("Device not found"); return
    for addr in range(0x100):
        val = regs.get(addr)
        if val is None:
            print(f"  [0x{addr:02X}] TIMEOUT"); continue
        asc = ''.join(chr(b) if 32 <= b < 127 else '.'
                      for b in struct.pack('<I', val))
        mark = _DUMP_MARKS.get(addr, '')
        print(f"  [0x{addr:02X}] 0x{val:08X} '{asc}'{mark}")


def cmd_read(args):
    try:
        with KT02H20() as dev:
            addr = _parse_int(args.addr)
            val = dev.read(addr)
    except ConnectionError:
        print("Device not found"); return
    if val is not None:
        asc = ''.join(chr(b) if 32 <= b < 127 else '.'
                      for b in struct.pack('<I', val))
        print(f"reg[0x{addr:04X}] = 0x{val:08X} ({val})  '{asc}'")
    else:
        print(f"reg[0x{addr:04X}] = (no response)")


def cmd_write(args):
    try:
        with KT02H20() as dev:
            addr = _parse_int(args.addr)
            val  = _parse_int(args.val)
            ok = dev.write(addr, val)
            print(f"Write 0x{addr:04X} = 0x{val:08X} → {'ACK' if ok else 'FAILED'}")
            if ok:
                time.sleep(0.01)
                rv = dev.read(addr)
                if rv is not None:
                    ok2 = '✓' if rv == val else f'✗ (read 0x{rv:08X})'
                    print(f"  Read-back: 0x{rv:08X} {ok2}")
    except ConnectionError:
        print("Device not found"); return


def cmd_ng(args):
    """Read + optionally write NoiseGate parameters."""
    try:
        with KT02H20() as dev:
            if args.set_param:
                p = args.set_param
                val_map = {'en': lambda v: v != 0,
                           'gatevol': int, 'thhigh': int, 'thlow': int,
                           'at': int, 'rt': int, 'noiset': int}
                try:
                    new_val = val_map[p](int(args.value))
                except (ValueError, KeyError):
                    print(f"Invalid value: {args.value}"); return
                if not dev.set_ng(**{p: new_val}):
                    print("Failed to write NG registers"); return

            ng = dev.get_ng()
    except ConnectionError:
        print("Device not found"); return

    if ng is None:
        print("Failed to read NG registers"); return

    print("── NoiseGate ──")
    print(f"  0x{REG_NG_BASE:02X}: EN={'ON' if ng['en'] else 'OFF'}  "
          f"GateVol={ng['gatevol']:+d} dB  THhigh={ng['thhigh']:+d} dB  "
          f"THlow={ng['thlow']:+d} dB  flags=0x{ng['flags']:02X}")
    print(f"  0x{REG_NG_BASE+1:02X}: RT={ng['rt']} ms  AT={ng['at']} ms")
    print(f"  0x{REG_NG_BASE+2:02X}: noiseT={ng['noiset']} ms  hold=0x{ng['hold']:04X}")


def cmd_lim(args):
    """Read + optionally write Limiter parameters."""
    try:
        with KT02H20() as dev:
            if args.set_param:
                p = args.set_param
                val_map = {'en': lambda v: v != 0, 'soft': lambda v: v != 0,
                           'point': int, 'at': int, 'rt': int}
                try:
                    new_val = val_map[p](int(args.value))
                except (ValueError, KeyError):
                    print(f"Invalid value: {args.value}"); return
                if not dev.set_lim(**{p: new_val}):
                    print("Failed to write Limiter registers"); return

            lim = dev.get_lim()
    except ConnectionError:
        print("Device not found"); return

    if lim is None:
        print("Failed to read Limiter registers"); return

    print("── Limiter ──")
    print(f"  0x{REG_LIM_BASE:02X}: EN={'ON' if lim['en'] else 'OFF'}  "
          f"SOFT={'ON' if lim['soft'] else 'OFF'}  Point={lim['point']:+d} dB")
    print(f"  0x{REG_LIM_BASE+1:02X}: RT={lim['rt']} ms  AT={lim['at']} ms")


def cmd_vol(args):
    """Read + optionally write volume (PGA + digital gains)."""
    try:
        with KT02H20() as dev:
            if args.set_param:
                p = args.set_param
                try:
                    if p == 'pga_adc':
                        dev.set_pga_adc(_pga_resolve(PGA_ADC_GAINS, args.value))
                    elif p == 'pga_dac':
                        dev.set_pga_dac(_pga_resolve(PGA_DAC_GAINS, args.value))
                    elif p == 'dig_adc':
                        dev.set_dig_adc(float(args.value))
                    elif p == 'dig_dac':
                        dev.set_dig_dacl(float(args.value))  # single-DAC: byte0
                    elif p == 'dig_dacl':
                        dev.set_dig_dacl(float(args.value))
                    elif p == 'dig_dacr':
                        dev.set_dig_dacr(float(args.value))
                except ValueError as e:
                    print(f"Error: {e}"); return

            flags = dev.get_protocol_flags()
            single = dev.is_single_dac()
            pa = dev.get_pga_adc()
            pd = dev.get_pga_dac()
            da = dev.get_dig_adc()
            dacl = dev.get_dig_dacl()
            dacr = dev.get_dig_dacr()
    except ConnectionError:
        print("Device not found"); return

    print("── Volume ──")
    if pa is not None:
        g = PGA_ADC_GAINS[pa] if 0 <= pa < len(PGA_ADC_GAINS) else f'idx {pa}'
        print(f"  A_ADC  (0x3A): idx {pa} = {g}")
    if pd is not None:
        g = PGA_DAC_GAINS[pd] if 0 <= pd < len(PGA_DAC_GAINS) else f'idx {pd}'
        print(f"  A_DAC  (0x3B): idx {pd} = {g}")
    if da is not None:
        print(f"  DIG_ADC  (0x65): {da:+.1f} dB")
    if dacl is not None:
        if single:
            print(f"  DIG_DAC  (0x66 b0): {dacl:+.1f} dB   (single-DAC model)")
        else:
            dacr_s = f"{dacr:+.1f} dB" if dacr is not None else "?"
            print(f"  DIG_DACL (0x66 b0): {dacl:+.1f} dB   "
                  f"DIG_DACR (0x66 b1): {dacr_s}")


def cmd_eq(args):
    """Read + optionally write EQ bands."""
    ch = args.channel

    try:
        with KT02H20() as dev:
            if args.enable or args.disable:
                dev.set_eq_en(ch, args.enable)

            if args.set_band is not None:
                n = args.set_band
                if not (0 <= n < REG_EQ_BANDS):
                    print(f"Band {n} out of range 0..{REG_EQ_BANDS-1}"); return
                ftype = TYPE_NAMES[args.ftype] if args.ftype else None
                if not dev.set_eq_band(ch, n, freq=args.freq, gain=args.gain,
                                       ftype=ftype, q=args.q):
                    print("Failed to write EQ band"); return

            _print_eq_channel(dev, ch)
    except ConnectionError:
        print("Device not found"); return


def cmd_flash(args):
    if not os.path.isfile(args.firmware):
        print(f"Firmware file not found: {args.firmware}")
        return
    if not (0x00 <= args.chip_type <= 0xFF):
        print(f"Invalid chip type: 0x{args.chip_type:02X} (must be 0x00-0xFF)")
        return
    try:
        with KT02H20Boot() as dev:
            dev.flash(args.firmware, args.chip_type)
    except ConnectionError:
        print("Device not found (bootloader mode?)"); return


def main():
    p = argparse.ArgumentParser(description='KTMicro USB Audio Chip Tool')
    p.add_argument('-v', '--verbose', action='store_true', help='Verbose debug output')
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

    global _VERBOSE
    _VERBOSE = args.verbose

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
