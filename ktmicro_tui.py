#!/usr/bin/env python3
"""Interactive curses TUI for KT02H20 EQ control with live response graph.

Layout:
  line 0-1 : status header (chip info + HID path)
  table    : 5-band EQ for selected channel, editable cells
  graph    : ASCII approximation of the summed biquad frequency response
  bottom   : key help + last write status

Edits are written to the device immediately (like the vendor app).
Use --demo to run without hardware.
"""

import argparse
import curses
import math

from typing import Optional

from kt02h20 import (KT02H20, FILTER_TYPES, REG_EQ_BANDS,
                     REG_MANUF, REG_PRODUCT, REG_VERSION, REG_SERIAL, REG_FLAGS)

# Assumed DSP sample rate for the response approximation (USB audio default)
FS = 48000.0
FMIN, FMAX = 20.0, 20000.0
DB_RANGE = 12.0

_FREQ_STEP = 1.122    # ~1/6 octave per keystroke
_Q_STEP = 1.2
_GAIN_STEP = 0.5


# ── Biquad math (RBJ Audio EQ Cookbook) ────────────────────────

def _biquad_coeffs(ftype: int, freq: float, gain_db: float, q: float,
                   fs: float = FS) -> tuple:
    """Return (b0,b1,b2,a0,a1,a2). Passthrough for invalid params."""
    if freq <= 0 or q <= 0:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    f = min(freq, fs / 2 * 0.99)
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * f / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2.0 * q)

    if ftype == 0:      # Peaking
        b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    elif ftype == 1:    # LPF
        b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif ftype == 2:    # HPF
        b0, b1, b2 = (1 + cw) / 2, -(1 + cw), (1 + cw) / 2
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif ftype == 3:    # Low shelf
        sA = math.sqrt(A)
        b0 = A * ((A + 1) - (A - 1) * cw + 2 * sA * alpha)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - 2 * sA * alpha)
        a0 = (A + 1) + (A - 1) * cw + 2 * sA * alpha
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - 2 * sA * alpha
    elif ftype == 4:    # High shelf
        sA = math.sqrt(A)
        b0 = A * ((A + 1) + (A - 1) * cw + 2 * sA * alpha)
        b1 = -2 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - 2 * sA * alpha)
        a0 = (A + 1) - (A - 1) * cw + 2 * sA * alpha
        a1 = 2 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - 2 * sA * alpha
    else:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return (b0, b1, b2, a0, a1, a2)


def _biquad_db(coeffs: tuple, freq: float, fs: float = FS) -> float:
    """Magnitude response in dB of a biquad at freq."""
    b0, b1, b2, a0, a1, a2 = coeffs
    w = 2.0 * math.pi * freq / fs
    c1, s1 = math.cos(w), math.sin(w)
    c2, s2 = math.cos(2 * w), math.sin(2 * w)
    n_re = b0 + b1 * c1 + b2 * c2
    n_im = -(b1 * s1 + b2 * s2)
    d_re = a0 + a1 * c1 + a2 * c2
    d_im = -(a1 * s1 + a2 * s2)
    n2 = n_re * n_re + n_im * n_im
    d2 = d_re * d_re + d_im * d_im
    if n2 <= 0 or d2 <= 0:
        return 0.0
    return 10.0 * math.log10(n2 / d2)


def band_response_db(band: dict, freq: float, fs: float = FS) -> float:
    """Response of one EQ band dict {freq,gain,ftype,q} at freq Hz."""
    if not band or band.get('freq', 0) <= 0:
        return 0.0
    return _biquad_db(_biquad_coeffs(band['ftype'], band['freq'],
                                     band['gain'], band['q'], fs), freq, fs)


def total_response_db(bands, freq: float, enabled: bool = True, fs: float = FS) -> float:
    """Summed response of all bands (0 dB if EQ disabled)."""
    if not enabled:
        return 0.0
    return sum(band_response_db(b, freq, fs) for b in bands if b)


# ── Demo device (no hardware) ──────────────────────────────────

class _MockDevice:
    """Same EQ interface as KT02H20, backed by memory."""

    hid_path = 'demo'

    def __init__(self):
        self._bands = {
            'adc': [
                {'freq': 120,   'gain': 4.0,  'ftype': 3, 'q': 0.9},
                {'freq': 1000,  'gain': -2.5, 'ftype': 0, 'q': 1.4},
                {'freq': 3500,  'gain': 2.0,  'ftype': 0, 'q': 2.0},
                {'freq': 8000,  'gain': -3.0, 'ftype': 0, 'q': 1.1},
                {'freq': 12000, 'gain': 5.0,  'ftype': 4, 'q': 0.8},
            ],
            'dac': [{'freq': 0, 'gain': 0.0, 'ftype': 0, 'q': 0.7}
                    for _ in range(REG_EQ_BANDS)],
        }
        self._en = {'adc': True, 'dac': False}

    def get_eq_en(self, ch):
        return self._en[ch]

    def set_eq_en(self, ch, en):
        self._en[ch] = bool(en)
        return True

    def get_eq_band(self, ch, n):
        return dict(self._bands[ch][n])

    def set_eq_band(self, ch, n, **kw):
        self._bands[ch][n].update({k: v for k, v in kw.items() if v is not None})
        return True

    def close(self):
        pass


# ── TUI application ────────────────────────────────────────────

class EqTui:
    COLS = ('Type', 'Freq', 'Gain', 'Q')
    # x positions of table cells: (type, freq, gain, q)
    _CELL_X = (6, 13, 24, 34)

    def __init__(self, dev, header: dict):
        self.dev = dev
        self.header = header
        self.ch = 'adc'
        self.sel_band = 0
        self.sel_col = 0
        self.msg = ''
        self.bands = {'adc': [], 'dac': []}
        self.en = {'adc': False, 'dac': False}
        self._load()

    # ---- device sync ----

    def _load(self):
        for ch in ('adc', 'dac'):
            self.en[ch] = bool(self.dev.get_eq_en(ch))
            self.bands[ch] = [
                self.dev.get_eq_band(ch, i)
                or {'freq': 0, 'gain': 0.0, 'ftype': 0, 'q': 0.7}
                for i in range(REG_EQ_BANDS)]
        self.msg = 'reloaded'

    def _adjust(self, steps: int):
        b = self.bands[self.ch][self.sel_band]
        col = self.sel_col
        if col == 0:
            b['ftype'] = (b['ftype'] + steps) % len(FILTER_TYPES)
            kw = {'ftype': b['ftype']}
        elif col == 1:
            f = b['freq'] or 100
            b['freq'] = int(min(24000, max(10, round(f * _FREQ_STEP ** steps))))
            kw = {'freq': b['freq']}
        elif col == 2:
            g = b['gain'] + _GAIN_STEP * steps
            b['gain'] = max(-30.0, min(30.0, round(g * 2) / 2))
            kw = {'gain': b['gain']}
        else:
            b['q'] = round(max(0.1, min(20.0, b['q'] * _Q_STEP ** steps)), 3)
            kw = {'q': b['q']}
        ok = self.dev.set_eq_band(self.ch, self.sel_band, **kw)
        self.msg = f"write {'OK' if ok else 'FAILED'}"

    def _toggle_en(self):
        self.en[self.ch] = not self.en[self.ch]
        ok = self.dev.set_eq_en(self.ch, self.en[self.ch])
        self.msg = f"EQ {'en' if self.en[self.ch] else 'dis'}abled " \
                   f"({'OK' if ok else 'FAILED'})"

    # ---- drawing ----

    @staticmethod
    def _put(scr, y, x, s, attr=0):
        try:
            scr.addnstr(y, x, s, max(0, scr.getmaxyx()[1] - x - 1), attr)
        except curses.error:
            pass

    def _draw_header(self, scr, w):
        h = self.header
        self._put(scr, 0, 1,
                  f"{h['vendor']} {h['product']}  ver {h['version']}  "
                  f"s/n {h['serial']}  [{h['model']}]",
                  curses.A_REVERSE | curses.A_BOLD)
        self._put(scr, 0, max(1, w - len(h['path']) - 2), h['path'],
                  curses.A_REVERSE | curses.A_BOLD)
        en = 'ON' if self.en[self.ch] else 'OFF'
        other = 'DAC' if self.ch == 'adc' else 'ADC'
        self._put(scr, 1, 1,
                  f"Channel: {self.ch.upper()}  (Tab: switch to {other})   "
                  f"EQ: {en}  (e: toggle)   fs≈48 kHz (graph is an approximation)")

    def _draw_table(self, scr, top):
        self._put(scr, top, 4, '  '.join(f'{c:<8}' for c in self.COLS), curses.A_BOLD)
        for i, b in enumerate(self.bands[self.ch]):
            y = top + 1 + i
            t = FILTER_TYPES.get(b['ftype'], str(b['ftype']))
            cells = (t, f"{b['freq']} Hz", f"{b['gain']:+.1f} dB", f"{b['q']:.3f}")
            mark = '>' if i == self.sel_band else ' '
            self._put(scr, y, 0, mark)
            self._put(scr, y, 2, str(i))
            for ci, (x, val) in enumerate(zip(self._CELL_X, cells)):
                attr = curses.A_REVERSE if (i == self.sel_band and ci == self.sel_col) else 0
                self._put(scr, y, x, val, attr)

    def _draw_graph(self, scr, top, height, width):
        left = 7
        ncols = width - left - 1
        if ncols < 10 or height < 5:
            self._put(scr, top, 1, "(terminal too small for graph)")
            return
        bands = self.bands[self.ch]
        en = self.en[self.ch]

        def db_to_row(db):
            frac = (DB_RANGE - db) / (2 * DB_RANGE)
            return max(0, min(height - 1, int(round(frac * (height - 1)))))

        def col_freq(c):
            return FMIN * (FMAX / FMIN) ** (c / (ncols - 1))

        # dB axis labels
        for db in (12, 6, 0, -6, -12):
            self._put(scr, top + db_to_row(db), 0, f"{db:+4d} │")
        # 0 dB line + decade gridlines
        zero = db_to_row(0.0)
        grid_cols = {}
        for f in (100, 1000, 10000):
            grid_cols[int(round(math.log(f / FMIN) / math.log(FMAX / FMIN) * (ncols - 1)))] = f
        for c in range(ncols):
            ch = '·' if c in grid_cols else '─'
            self._put(scr, top + zero, left + c, ch, curses.A_DIM)
        for gc in grid_cols:
            for r in range(height):
                if r != zero:
                    self._put(scr, top + r, left + gc, '┆', curses.A_DIM)
        # curve
        prev = None
        for c in range(ncols):
            db = total_response_db(bands, col_freq(c), en)
            db = max(-DB_RANGE, min(DB_RANGE, db))
            r = db_to_row(db)
            if prev is not None:
                for rr in range(min(r, prev), max(r, prev) + 1):
                    self._put(scr, top + rr, left + c, '│' if rr != r else '●')
            self._put(scr, top + r, left + c, '●')
            prev = r
        # frequency labels
        fy = top + height
        self._put(scr, fy, left, '20')
        for gc, f in grid_cols.items():
            self._put(scr, fy, left + gc - 1, '1k' if f == 1000 else ('10k' if f == 10000 else '100'))
        self._put(scr, fy, left + ncols - 3, '20k')

    def _draw_status(self, scr, y):
        self._put(scr, y, 1,
                  "Tab:ch ↑↓:band ←→:param +/-:adj e:EQ r:reload q:quit",
                  curses.A_DIM)
        if self.msg:
            self._put(scr, y, max(1, scr.getmaxyx()[1] - len(self.msg) - 2),
                      self.msg, curses.A_BOLD)

    def _draw(self, scr):
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 20 or w < 55:
            self._put(scr, 0, 0, f"Terminal too small ({w}x{h}), need >=55x20")
            scr.refresh()
            return
        self._draw_header(scr, w)
        self._draw_table(scr, 3)
        gtop = 3 + 1 + REG_EQ_BANDS + 1
        gheight = h - gtop - 3
        self._draw_graph(scr, gtop, gheight, w)
        self._draw_status(scr, h - 1)
        scr.refresh()

    # ---- main loop ----

    def run(self, scr):
        curses.curs_set(0)
        scr.keypad(True)        # decode arrows / PgUp / PgDn into KEY_* codes
        scr.timeout(-1)
        while True:
            self._draw(scr)
            k = scr.getch()
            if k in (ord('q'), ord('Q'), 27):
                return
            elif k in (9, ord('c')):                    # Tab / c
                self.ch = 'dac' if self.ch == 'adc' else 'adc'
                self.sel_band = min(self.sel_band, REG_EQ_BANDS - 1)
            elif k == curses.KEY_UP:
                self.sel_band = (self.sel_band - 1) % REG_EQ_BANDS
            elif k == curses.KEY_DOWN:
                self.sel_band = (self.sel_band + 1) % REG_EQ_BANDS
            elif k == curses.KEY_LEFT:
                self.sel_col = (self.sel_col - 1) % len(self.COLS)
            elif k == curses.KEY_RIGHT:
                self.sel_col = (self.sel_col + 1) % len(self.COLS)
            elif k in (ord('+'), ord('=')):
                self._adjust(1)
            elif k == ord('-'):
                self._adjust(-1)
            elif k == curses.KEY_PPAGE:
                self._adjust(10)
            elif k == curses.KEY_NPAGE:
                self._adjust(-10)
            elif k == ord('e'):
                self._toggle_en()
            elif k == ord('r'):
                self._load()
            elif k == curses.KEY_RESIZE:
                pass                                     # redraw on next iteration


# ── startup helpers ────────────────────────────────────────────

def _read_header(dev) -> dict:
    info = dev.info()
    flags = info.get(REG_FLAGS)
    return {
        'vendor':  info.get(REG_MANUF, '?') or '?',
        'product': info.get(REG_PRODUCT, '?') or '?',
        'version': info.get(REG_VERSION, '?') or '?',
        'serial':  info.get(REG_SERIAL, '?') or '?',
        'model':   'single-DAC' if (flags or 0) & 0x200 else 'stereo-DAC',
        'path':    dev.hid_path or '?',
    }


_DEMO_HEADER = {
    'vendor': 'KTMicro', 'product': 'KT02H20 (DEMO)', 'version': '1.0.17',
    'serial': 'DEMO0001', 'model': 'stereo-DAC', 'path': 'demo',
}


def main() -> int:
    ap = argparse.ArgumentParser(description='KT02H20 interactive EQ TUI')
    ap.add_argument('--demo', action='store_true',
                    help='run with a mock device (no hardware)')
    args = ap.parse_args()

    if args.demo:
        dev, header = _MockDevice(), _DEMO_HEADER
    else:
        dev = KT02H20()
        if not dev.open():
            print("KT02H20 device not found")
            return 1
        header = _read_header(dev)

    try:
        curses.wrapper(lambda scr: EqTui(dev, header).run(scr))
    finally:
        dev.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
