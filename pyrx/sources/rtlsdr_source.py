"""RTL-SDR (RTL2832U + R820T/R828D and others) via pyrtlsdr.

pyrtlsdr is used for enumeration, open/close and the async read loop. Control calls go
straight to the librtlsdr function table pyrtlsdr exposes, because several pyrtlsdr
wrappers close the device on any error (or raise NameError, see set_freq_correction in
0.3.0), which would turn a harmless "not supported" into a dead device."""

from __future__ import annotations

import ctypes
import logging
import re
import sys
import threading
import types
import warnings

import numpy as np

from ._dlls import add_windows_dll_dirs
from .base import Control, DeviceInfo, Source, SourceError

log = logging.getLogger(__name__)

_rtl = None
_IMPORT_ERROR = ""

TUNERS = {0: "Unknown", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}
TUNER_RANGES = {
    1: (52e6, 2200e6), 2: (22e6, 948e6), 3: (22e6, 1100e6), 4: (146e6, 924e6),
    5: (24e6, 1766e6), 6: (24e6, 1766e6),
}
DIRECT_SAMPLING_RANGE = (0.5e6, 28.8e6)
RATES = [0.25e6, 1.024e6, 1.4e6, 1.8e6, 1.92e6, 2.048e6, 2.4e6, 2.56e6, 2.88e6, 3.2e6]


def _load():
    """Import pyrtlsdr once. Returns the module or None (reason in _IMPORT_ERROR)."""
    global _rtl, _IMPORT_ERROR
    if _rtl is not None or _IMPORT_ERROR:
        return _rtl
    add_windows_dll_dirs()
    try:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import pkg_resources  # noqa: F401  (pyrtlsdr 0.3.0 imports it for its version string)
        except ImportError:
            stub = types.ModuleType("pkg_resources")

            def _require(*_a, **_k):
                raise RuntimeError("pkg_resources stub")
            stub.require = _require
            sys.modules["pkg_resources"] = stub
        import rtlsdr  # noqa: F401
        from rtlsdr import RtlSdr  # noqa: F401
        from rtlsdr.librtlsdr import librtlsdr, p_rtlsdr_dev
        if hasattr(librtlsdr, "rtlsdr_set_offset_tuning"):  # not wrapped by pyrtlsdr 0.3.0
            f = librtlsdr.rtlsdr_set_offset_tuning
            f.restype, f.argtypes = ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]
        _rtl = rtlsdr
    except AttributeError as e:
        _IMPORT_ERROR = (f"pyrtlsdr does not match the installed librtlsdr ({e}). "
                         "Install pyrtlsdr==0.3.0 (see requirements-hw.txt).")
    except Exception as e:  # ImportError: no pyrtlsdr / no librtlsdr shared library
        _IMPORT_ERROR = f"RTL-SDR support unavailable: {e}"
    return _rtl


def _lib():
    from rtlsdr.librtlsdr import librtlsdr
    return librtlsdr


def _open_hint(err: Exception) -> str:
    s = str(err)
    if "-3" in s or "ACCESS" in s.upper():
        return " Permission denied: install the udev rules (see README) and re-plug the device."
    if "-6" in s or "BUSY" in s.upper():
        return " The device is busy: another program, or the kernel DVB driver (dvb_usb_rtl28xxu), has it."
    return (" Check that it is plugged in, not used by another program, and (Linux) that the "
            "dvb_usb_rtl28xxu driver is blacklisted; (Windows) that WinUSB is installed with Zadig.")


class RtlSdrSource(Source):
    name = "rtlsdr"
    display_name = "RTL-SDR"

    def __init__(self):
        self._gains: list[float] = []
        self._tuner = 0
        super().__init__()
        self._rate = 2.048e6
        self._center = 100e6
        self.sdr = None
        self._thread: threading.Thread | None = None
        self._stop_requested = False
        self.device_label = ""

    # ------------------------------------------------------------------ discovery
    @classmethod
    def available(cls):
        ok = _load() is not None
        return ok, ("" if ok else _IMPORT_ERROR)

    @classmethod
    def enumerate(cls):
        if _load() is None:
            return []
        lib = _lib()
        out = []
        try:
            n = lib.rtlsdr_get_device_count()
            for i in range(n):
                name = lib.rtlsdr_get_device_name(i)
                name = name.decode(errors="replace") if isinstance(name, bytes) else str(name or "RTL-SDR")
                manu = (ctypes.c_ubyte * 256)()
                prod = (ctypes.c_ubyte * 256)()
                ser = (ctypes.c_ubyte * 256)()
                r = lib.rtlsdr_get_device_usb_strings(i, manu, prod, ser)
                serial = bytes(b for b in ser if b).decode(errors="replace") if r == 0 else "?"
                out.append(DeviceInfo(str(i), f"#{i} {name}  SN: {serial}", serial))
        except Exception as e:
            log.warning("RTL-SDR enumeration failed: %s", e)
        return out

    # ------------------------------------------------------------------ controls
    def controls(self):
        gains = self._gains or [0.0]
        gchoices = [(g, f"{g:.1f} dB") for g in gains]
        return [
            Control("info", "Tuner", "info", tooltip="Tuner chip reported by the device"),
            Control("tuner_agc", "Tuner AGC", "bool", False,
                    tooltip="Let the tuner choose its gain automatically"),
            Control("gain", "RF gain", "slider", gains[len(gains) // 2], choices=gchoices,
                    enabled_when=("tuner_agc", False), tooltip="Tuner gain (values read from the device)"),
            Control("rtl_agc", "RTL AGC", "bool", False, tooltip="RTL2832U digital AGC"),
            Control("ppm", "Frequency correction", "number", 0, minimum=-200, maximum=200, step=1, unit="ppm"),
            Control("direct_sampling", "Direct sampling (HF)", "choice", 0,
                    choices=[(0, "Off (quadrature)"), (1, "I branch"), (2, "Q branch")],
                    tooltip="Bypass the tuner and sample HF directly (0.5-28.8 MHz). "
                            "RTL-SDR Blog V3 uses the Q branch."),
            Control("offset_tuning", "Offset tuning", "bool", False,
                    tooltip="Tuner-side LO offset (E4000 and some FC tuners only; R820T/R828D do not "
                            "support it - use 'LO offset' in the Source panel instead)"),
            Control("bias_tee", "Bias-T (antenna power)", "bool", False,
                    confirm="Enable the bias-T? This puts ~4.5 V DC on the antenna connector. "
                            "Only do this with an LNA or active antenna that expects it - it can "
                            "damage equipment that is DC-shorted.",
                    tooltip="Supplies DC power to an active antenna / LNA (RTL-SDR Blog V3/V4)"),
        ]

    def info_text(self):
        return f"{self.device_label}\nTuner: {TUNERS.get(self._tuner, '?')}" if self.sdr else ""

    def _check(self, r: int, what: str, allow=(0,)):
        if r < 0 and r not in allow:
            raise SourceError(f"RTL-SDR: could not {what} (error {r})")
        return r

    def _apply_control(self, key, value):
        if self.sdr is None:
            return  # stored, applied on open
        lib, dev = _lib(), self.sdr.dev_p
        if key == "tuner_agc":
            self._check(lib.rtlsdr_set_tuner_gain_mode(dev, 0 if value else 1), "set tuner gain mode")
            if not value:
                self._apply_control("gain", self._values.get("gain", 0.0))
        elif key == "gain":
            if self._values.get("tuner_agc"):
                return
            if self._gains:
                g = min(self._gains, key=lambda x: abs(x - float(value)))
                self._check(lib.rtlsdr_set_tuner_gain_mode(dev, 1), "set manual gain mode")
                self._check(lib.rtlsdr_set_tuner_gain(dev, int(round(g * 10))), "set gain")
        elif key == "rtl_agc":
            self._check(lib.rtlsdr_set_agc_mode(dev, 1 if value else 0), "set RTL AGC")
        elif key == "ppm":
            # -2 means "unchanged" in librtlsdr
            self._check(lib.rtlsdr_set_freq_correction(dev, int(value)), "set frequency correction", (0, -2))
        elif key == "direct_sampling":
            self._check(lib.rtlsdr_set_direct_sampling(dev, int(value)), "set direct sampling")
            self._values["direct_sampling"] = int(value)
            lo, hi = self.frequency_range()
            self._hw_set_center(min(max(self._center, lo), hi))
        elif key == "offset_tuning":
            r = lib.rtlsdr_set_offset_tuning(dev, 1 if value else 0)
            if r < 0:
                raise SourceError(f"Offset tuning is not supported by the {TUNERS.get(self._tuner, '?')} tuner")
        elif key == "bias_tee":
            if not hasattr(lib, "rtlsdr_set_bias_tee"):
                raise SourceError("This librtlsdr has no bias-T support")
            self._check(lib.rtlsdr_set_bias_tee(dev, 1 if value else 0), "switch bias-T")

    # ------------------------------------------------------------------ device
    def open(self, device_id=None):
        if _load() is None:
            raise SourceError(_IMPORT_ERROR)
        from rtlsdr import RtlSdr
        idx = int(device_id) if device_id not in (None, "") else 0
        devs = self.enumerate()
        if not devs:
            raise SourceError("No RTL-SDR device found." + _open_hint(Exception("")))
        try:
            self.sdr = RtlSdr(device_index=idx)
        except Exception as e:
            self.sdr = None
            raise SourceError(f"Could not open RTL-SDR #{idx}: {e}." + _open_hint(e)) from None
        lib, dev = _lib(), self.sdr.dev_p
        self.device_label = next((d.label for d in devs if d.id == str(idx)), f"#{idx}")
        try:
            self._tuner = int(lib.rtlsdr_get_tuner_type(dev))
            buf = (ctypes.c_int * 64)()
            n = lib.rtlsdr_get_tuner_gains(dev, buf)
            self._gains = sorted(buf[i] / 10.0 for i in range(max(n, 0)))
            self._check(lib.rtlsdr_set_sample_rate(dev, int(self._rate)), "set sample rate")
            lib.rtlsdr_reset_buffer(dev)
            self._open = True
            # apply stored settings (defaults or restored values)
            for key in ("direct_sampling", "tuner_agc", "gain", "rtl_agc", "ppm", "bias_tee", "offset_tuning"):
                v = self._values.get(key)
                if key == "gain" and self._gains and v not in self._gains:
                    v = self._gains[len(self._gains) // 2] if v is None else min(self._gains, key=lambda g: abs(g - float(v)))
                    self._values["gain"] = v
                if v is None or (key in ("offset_tuning", "bias_tee") and not v):
                    continue  # off is the power-on default; R82xx would log "unsupported"
                try:
                    self._apply_control(key, v)
                except SourceError as e:
                    log.warning("%s", e)
                    if key in ("offset_tuning", "bias_tee"):
                        self._values[key] = False
            self._hw_set_center(self._center)
        except Exception:
            self.close()
            raise

    def close(self):
        self.stop()
        if self.sdr is not None:
            if self._values.get("bias_tee"):
                # the bias-T GPIO survives rtlsdr_close: never leave antenna power on
                try:
                    _lib().rtlsdr_set_bias_tee(self.sdr.dev_p, 0)
                except Exception as e:
                    log.warning("could not switch bias-T off: %s", e)
                self._values["bias_tee"] = False
            try:
                self.sdr.close()
            except Exception as e:
                log.warning("RTL-SDR close: %s", e)
        self.sdr = None
        self._open = False

    def frequency_range(self):
        if self._values.get("direct_sampling"):
            return DIRECT_SAMPLING_RANGE
        lo, hi = TUNER_RANGES.get(self._tuner, (24e6, 1766e6))
        if self._tuner == 6:
            lo = 0.5e6  # RTL-SDR Blog V4 (R828D): HF via built-in upconverter with the Blog driver
        return lo, hi

    def sample_rates(self):
        return list(RATES)

    def usable_bandwidth(self):
        return self._rate * 0.85

    def _hw_set_center(self, hz):
        if self.sdr is not None:
            r = _lib().rtlsdr_set_center_freq(self.sdr.dev_p, int(round(hz)))
            if r < 0:
                raise SourceError(f"RTL-SDR could not tune to {hz / 1e6:.6f} MHz (error {r})")
        self._center = float(hz)
        return self._center

    def _hw_set_rate(self, hz):
        if self.sdr is not None:
            r = _lib().rtlsdr_set_sample_rate(self.sdr.dev_p, int(round(hz)))
            if r < 0:
                raise SourceError(f"RTL-SDR rejected sample rate {hz / 1e6:g} MS/s")
        return float(hz)

    # ------------------------------------------------------------------ streaming
    def start(self, callback, error_callback=None):
        if self._running:
            return
        if self.sdr is None:
            raise SourceError("RTL-SDR is not open")
        self._callback = callback
        self._error_cb = error_callback
        self._stop_requested = False
        _lib().rtlsdr_reset_buffer(self.sdr.dev_p)
        self._running = True
        self._thread = threading.Thread(target=self._run, name="rtlsdr-read", daemon=True)
        self._thread.start()

    def stop(self):
        if not self._running:
            return
        self._stop_requested = True
        try:
            if self.sdr is not None:
                _lib().rtlsdr_cancel_async(self.sdr.dev_p)
        except Exception as e:
            log.warning("cancel_async: %s", e)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                log.error("RTL-SDR reader thread did not stop")
        self._thread = None
        self._running = False

    def _on_bytes(self, values, _ctx):
        if self._stop_requested:
            return
        raw = np.frombuffer(values, dtype=np.uint8).astype(np.float32)
        raw -= np.float32(127.5)
        raw *= np.float32(1 / 127.5)
        self._callback(raw.view(np.complex64), self._center, self._rate)

    def _run(self):
        # ~50 callbacks per second (low latency, smooth display), a multiple of 16 KiB
        nbytes = max(16384, int(self._rate * 2 / 50) // 16384 * 16384)
        err = None
        try:
            self.sdr.read_bytes_async(self._on_bytes, num_bytes=nbytes)
        except Exception as e:
            err = e
        self._running = False
        if not self._stop_requested:
            detail = ""
            if err is not None:
                m = re.search(r"LIBUSB_ERROR_\w+", str(err))
                detail = f" ({m.group(0) if m else err})"
            self._report_error(f"RTL-SDR stopped streaming{detail}. The device was disconnected "
                               "or stopped responding.")
