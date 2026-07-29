# ktmicro-tools - KT02H20 USB Audio DSP tools

Python tools for the **KTMicro KT02H20** USB Audio DSP chip for dongles like JCALLY JM12 reverse-engineered from `KT_USB_APP.exe` protocol.

TUI demo:
<video src="demo.webm" controls muted loop></video>

## Features

- **`ktmicro.py`** - CLI for the full DSP register set: volume (PGA + digital gains),
  5-band PEQ per channel (ADC/DAC), NoiseGate, Limiter, state dump, register read/write
- **`ktmicro_tui.py`** - interactive curses TUI with live biquad frequency response graph
  (`--demo` mode works without hardware)
- **`kt02h20.py`** - device driver class with semantic get/set for every parameter
- **`kt02h20_boot.py`** - bootloader firmware flasher (HID feature reports)
- **`hid_transport.py`** - low-level HID transport (Linux `/dev/hidraw`)

Requires Python 3 and `hidapi` (`pip install hidapi`). Linux only.

## Quick start

```bash
# Read full DSP state
python3 ktmicro.py state

# Read / set EQ band
python3 ktmicro.py eq adc --set 0 --freq 1000 --gain +3.0 --q 1.0

# Toggle NoiseGate
python3 ktmicro.py ng --set en 0

# Interactive TUI
python3 ktmicro_tui.py 
```

## Tests

```bash
python3 -m unittest test_ktmicro -v
```

See `../KT_USB_PROTOCOL.md` for the complete register map and protocol documentation.
