"""HackRF One, receive only (see _hackrf_ctypes: no TX function is even bound)."""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time

import numpy as np

from . import _hackrf_ctypes as hr
from .base import Control, DeviceInfo, Source, SourceError

log = logging.getLogger(__name__)

RATES = [2e6, 4e6, 5e6, 8e6, 10e6, 12.5e6, 16e6, 20e6]
BB_FILTERS = [1.75e6, 2.5e6, 3.5e6, 5e6, 5.5e6, 6e6, 7e6, 8e6, 9e6, 10e6, 12e6, 14e6, 15e6, 20e6, 24e6, 28e6]


class HackRFSource(Source):
    name = "hackrf"
    display_name = "HackRF One"

    def __init__(self):
        super().__init__()
        self._rate = 10e6
        self._center = 100e6
        self._dev = None
        self._cb_ref = None
        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._worker: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._stopping = False
        self.serial = ""
        self.board = ""
        self.firmware = ""

    # ------------------------------------------------------------------ discovery
    @classmethod
    def available(cls):
        ok = hr.load() is not None
        return ok, ("" if ok else f"HackRF support unavailable: {hr.error()}")

    @classmethod
    def enumerate(cls):
        try:
            serials = hr.list_serials()
        except Exception as e:
            log.warning("HackRF enumeration failed: %s", e)
            return []
        return [DeviceInfo(s, f"HackRF  SN: {s.lstrip('0')[-16:] or s}", s) for s in serials]

    # ------------------------------------------------------------------ controls
    def controls(self):
        return [
            Control("info", "Board", "info"),
            Control("bb_filter", "Baseband filter", "choice", "auto",
                    choices=[("auto", "Auto (75 % of sample rate)")] + [(f, f"{f / 1e6:g} MHz") for f in BB_FILTERS],
                    tooltip="MAX2837 anti-alias filter bandwidth"),
            Control("lna_gain", "LNA (IF) gain", "slider", 16, minimum=0, maximum=40, step=8, unit="dB"),
            Control("vga_gain", "VGA (baseband) gain", "slider", 20, minimum=0, maximum=62, step=2, unit="dB"),
            Control("amp", "RF amplifier (+14 dB)", "bool", False,
                    tooltip="Front-end amplifier. Avoid with strong signals or high input levels "
                            "(max. input -5 dBm with amp on)."),
            Control("antenna_power", "Antenna port power (3.3 V)", "bool", False,
                    confirm="Enable antenna port power? This supplies 3.3 V DC on the HackRF's antenna "
                            "connector. Only use it with an active antenna or LNA that needs it - it can "
                            "damage equipment that is DC-shorted (e.g. some filters or loop antennas).",
                    tooltip="Bias voltage for active antennas / LNAs"),
        ]

    def info_text(self):
        if self._dev is None:
            return ""
        return f"{self.board}  firmware {self.firmware}\nSN {self.serial}"

    def _check(self, r: int, what: str):
        if r != hr.HACKRF_SUCCESS:
            raise SourceError(f"HackRF: could not {what}: {hr.error_name(r)}")

    def _bb_filter_value(self) -> int:
        v = self._values.get("bb_filter", "auto")
        lib = hr.load()
        if v in (None, "auto"):
            return int(lib.hackrf_compute_baseband_filter_bw(int(self._rate * 0.75)))
        return int(lib.hackrf_compute_baseband_filter_bw(int(float(v))))

    def _apply_control(self, key, value):
        if self._dev is None:
            return
        lib = hr.load()
        if key == "bb_filter":
            self._values["bb_filter"] = value
            self._check(lib.hackrf_set_baseband_filter_bandwidth(self._dev, self._bb_filter_value()),
                        "set baseband filter")
        elif key == "lna_gain":
            v = int(min(max(round(float(value) / 8) * 8, 0), 40))
            self._check(lib.hackrf_set_lna_gain(self._dev, v), "set LNA gain")
        elif key == "vga_gain":
            v = int(min(max(round(float(value) / 2) * 2, 0), 62))
            self._check(lib.hackrf_set_vga_gain(self._dev, v), "set VGA gain")
        elif key == "amp":
            self._check(lib.hackrf_set_amp_enable(self._dev, 1 if value else 0), "switch RF amplifier")
        elif key == "antenna_power":
            self._check(lib.hackrf_set_antenna_enable(self._dev, 1 if value else 0), "switch antenna power")

    # ------------------------------------------------------------------ device
    def open(self, device_id=None):
        lib = hr.load()
        if lib is None:
            raise SourceError(f"HackRF support unavailable: {hr.error()}")
        serials = hr.list_serials()
        if not serials:
            raise SourceError("No HackRF found. Check the USB cable and (Linux) the udev rules; "
                              "(Windows) that the WinUSB driver is installed.")
        serial = device_id or serials[0]
        try:
            self._dev = hr.open_device(serial)
        except OSError as e:
            hint = " (busy: is another program using it?)" if "BUSY" in str(e).upper() else ""
            raise SourceError(f"Could not open HackRF {serial[-8:]}: {e.args[-1]}{hint}") from None
        self.serial = serial
        try:
            b = ctypes.c_uint8()
            if lib.hackrf_board_id_read(self._dev, ctypes.byref(b)) == hr.HACKRF_SUCCESS:
                self.board = lib.hackrf_board_id_name(b.value).decode(errors="replace")
            buf = ctypes.create_string_buffer(255)
            if lib.hackrf_version_string_read(self._dev, buf, 254) == hr.HACKRF_SUCCESS:
                self.firmware = buf.value.decode(errors="replace")
            self._open = True
            self._hw_set_rate(self._rate)
            self._hw_set_center(self._center)
            for key in ("lna_gain", "vga_gain", "amp", "antenna_power"):
                self._apply_control(key, self._values.get(key))
        except Exception:
            self.close()
            raise

    def close(self):
        self.stop()
        if self._dev is not None:
            try:
                # make sure the bias voltage is never left on
                hr.load().hackrf_set_antenna_enable(self._dev, 0)
                hr.load().hackrf_close(self._dev)
            except Exception as e:
                log.warning("HackRF close: %s", e)
        self._dev = None
        self._open = False

    def frequency_range(self):
        return 1e6, 6e9

    def sample_rates(self):
        return list(RATES)

    def sample_rate_range(self):
        return 2e6, 20e6

    def usable_bandwidth(self):
        return self._rate * 0.8

    def _hw_set_center(self, hz):
        if self._dev is not None:
            self._check(hr.load().hackrf_set_freq(self._dev, int(round(hz))), f"tune to {hz / 1e6:.6f} MHz")
        self._center = float(hz)
        return self._center

    def _hw_set_rate(self, hz):
        if self._dev is not None:
            lib = hr.load()
            self._check(lib.hackrf_set_sample_rate(self._dev, float(hz)), f"set sample rate {hz / 1e6:g} MS/s")
            self._rate = float(hz)
            self._check(lib.hackrf_set_baseband_filter_bandwidth(self._dev, self._bb_filter_value()),
                        "set baseband filter")
        return float(hz)

    # ------------------------------------------------------------------ streaming
    def _rx_callback(self, transfer_p):
        """Runs on libhackrf's USB thread: copy the buffer and return immediately."""
        if self._stopping:
            return 1
        t = transfer_p.contents
        data = ctypes.string_at(t.buffer, t.valid_length)
        try:
            self._q.put_nowait(data)
        except queue.Full:
            self.overflows += 1
        return 0

    def start(self, callback, error_callback=None):
        if self._running:
            return
        if self._dev is None:
            raise SourceError("HackRF is not open")
        self._callback = callback
        self._error_cb = error_callback
        self._stopping = False
        self._stop_evt.clear()
        while not self._q.empty():
            self._q.get_nowait()
        self._cb_ref = hr.sample_block_cb_fn(self._rx_callback)
        r = hr.load().hackrf_start_rx(self._dev, self._cb_ref, None)
        if r != hr.HACKRF_SUCCESS:
            raise SourceError(f"HackRF: could not start receiving: {hr.error_name(r)}")
        self._running = True
        self._worker = threading.Thread(target=self._run, name="hackrf-convert", daemon=True)
        self._worker.start()

    def stop(self):
        if not self._running:
            return
        self._stopping = True
        self._stop_evt.set()
        if self._dev is not None:
            r = hr.load().hackrf_stop_rx(self._dev)
            if r != hr.HACKRF_SUCCESS:
                log.warning("hackrf_stop_rx: %s", hr.error_name(r))
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=3.0)
        self._worker = None
        self._running = False

    def _run(self):
        lib = hr.load()
        last_check = time.monotonic()
        while not self._stop_evt.is_set():
            try:
                data = self._q.get(timeout=0.2)
            except queue.Empty:
                data = None
            if data:
                raw = np.frombuffer(data, dtype=np.int8).astype(np.float32)
                raw *= np.float32(1 / 128.0)
                self._callback(raw.view(np.complex64), self._center, self._rate)
            now = time.monotonic()
            if now - last_check > 0.5:
                last_check = now
                if not self._stopping and lib.hackrf_is_streaming(self._dev) != hr.HACKRF_TRUE:
                    self._running = False
                    self._report_error("HackRF stopped streaming. The device was disconnected or stopped responding.")
                    return
