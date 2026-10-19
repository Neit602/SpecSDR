"""IQ file playback: 2-channel WAV (8-bit unsigned, 16-bit, float32) and raw
.cu8 / .cs8 / .cs16 / .cf32. Paced in real time, optional loop, seekable."""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from ..core.wavio import FORMATS, RAW_EXTENSIONS, parse_iq_filename, read_wav_info, to_complex
from .base import Control, Source, SourceError


class FileMetadataMissing(SourceError):
    """Sample rate and/or centre frequency could not be determined; the UI asks the user."""

    def __init__(self, path, fmt, center, rate):
        super().__init__("Sample rate / centre frequency unknown for " + os.path.basename(path))
        self.path, self.fmt, self.center, self.rate = path, fmt, center, rate


def probe_file(path: str) -> dict:
    """Return {'fmt', 'center', 'rate', 'offset', 'frames', 'wav'} (center/rate may be None)."""
    if not os.path.isfile(path):
        raise SourceError(f"File not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    fc, fs = parse_iq_filename(path)
    if ext == ".wav":
        try:
            info = read_wav_info(path)
        except ValueError as e:
            raise SourceError(f"Cannot read {os.path.basename(path)}: {e}") from None
        if info.channels != 2:
            raise SourceError(f"{os.path.basename(path)} has {info.channels} channel(s); IQ needs 2 (I and Q)")
        return dict(fmt=info.fmt, center=fc if fc is not None else info.center, rate=float(info.sample_rate),
                    offset=info.data_offset, frames=info.frames, wav=True)
    fmt = RAW_EXTENSIONS.get(ext)
    size = os.path.getsize(path)
    comp = np.dtype(FORMATS[fmt][0]).itemsize if fmt else 1
    return dict(fmt=fmt, center=fc, rate=fs, offset=0, frames=size // (2 * comp), wav=False)


class IqFileSource(Source):
    name = "file"
    display_name = "IQ file"
    tunable = False

    def __init__(self):
        super().__init__()
        self.path: str | None = None
        self._fmt = "cf32"
        self._offset = 0
        self._frames = 0
        self._mm: np.ndarray | None = None
        self._pos = 0  # frame index of the next read
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # user-supplied overrides when the file name / header doesn't tell
        self.override_center: float | None = None
        self.override_rate: float | None = None
        self.override_fmt: str | None = None

    @classmethod
    def enumerate(cls):
        return []

    def controls(self):
        return [Control("loop", "Loop playback", "bool", True, tooltip="Restart at the beginning at end of file")]

    # ------------------------------------------------------------------ open/close
    def open(self, device_id=None):
        path = device_id or self.path
        if not path:
            raise SourceError("No IQ file selected")
        meta = probe_file(path)
        fmt = self.override_fmt or meta["fmt"]
        center = self.override_center if self.override_center is not None else meta["center"]
        rate = self.override_rate if self.override_rate is not None else meta["rate"]
        if fmt is None or center is None or not rate:
            raise FileMetadataMissing(path, fmt, center, rate)
        if fmt != meta["fmt"] and not meta["wav"]:
            comp = np.dtype(FORMATS[fmt][0]).itemsize
            meta["frames"] = os.path.getsize(path) // (2 * comp)
        if meta["frames"] <= 0:
            raise SourceError(f"{os.path.basename(path)} contains no samples")
        self.path = path
        self._fmt = fmt
        self._offset = meta["offset"]
        self._frames = meta["frames"]
        dt = FORMATS[fmt][0]
        try:
            self._mm = np.memmap(path, dtype=dt, mode="r", offset=self._offset, shape=(self._frames * 2,))
        except (OSError, ValueError) as e:
            raise SourceError(f"Cannot open {os.path.basename(path)}: {e}") from None
        self._center = float(center)
        self._rate = float(rate)
        self._pos = 0
        self._open = True

    def close(self):
        self.stop()
        self._mm = None
        self._open = False

    # ------------------------------------------------------------------ properties
    def frequency_range(self):
        return self._center, self._center

    def set_center_frequency(self, hz):
        return self._center  # a recording can't be retuned

    def sample_rates(self):
        return [self._rate]

    def set_sample_rate(self, hz):
        return self._rate

    def usable_bandwidth(self):
        return self._rate

    @property
    def duration(self) -> float:
        return self._frames / self._rate if self._rate else 0.0

    @property
    def position(self) -> float:
        return self._pos / self._rate if self._rate else 0.0

    def seek(self, seconds: float) -> None:
        with self._lock:
            self._pos = int(min(max(seconds, 0.0), self.duration) * self._rate) % max(self._frames, 1)

    def info_text(self):
        if not self.path:
            return ""
        return (f"{os.path.basename(self.path)}\n{self._fmt}, {self._rate / 1e6:g} MS/s, "
                f"centre {self._center / 1e6:.6f} MHz, {self.duration:.1f} s")

    # ------------------------------------------------------------------ reading
    def read_block(self, n: int) -> tuple[np.ndarray, bool]:
        """Next n samples (fewer at end of file when not looping). Returns (iq, eof)."""
        with self._lock:
            loop = bool(self._values.get("loop", True))
            parts, need, eof = [], n, False
            while need > 0:
                avail = self._frames - self._pos
                if avail <= 0:
                    if loop:
                        self._pos = 0
                        continue
                    eof = True
                    break
                take = min(avail, need)
                raw = self._mm[self._pos * 2:(self._pos + take) * 2]
                parts.append(to_complex(raw, self._fmt))
                self._pos += take
                need -= take
            x = np.concatenate(parts) if parts else np.zeros(0, np.complex64)
            return x, eof

    def start(self, callback, error_callback=None):
        if self._running:
            return
        if not self._open:
            raise SourceError("No IQ file open")
        self._callback = callback
        self._error_cb = error_callback
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, name="file-source", daemon=True)
        self._thread.start()

    def stop(self):
        if not self._running:
            return
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._running = False
        self._thread = None

    def _run(self):
        n = max(1024, int(self._rate / 50))
        next_t = time.monotonic()
        try:
            while not self._stop.is_set():
                x, eof = self.read_block(n)
                if len(x):
                    self._callback(x, self._center, self._rate)
                if eof:
                    self._running = False
                    self._report_error("End of file reached", "eof")
                    return
                next_t += len(x) / self._rate
                delay = next_t - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                elif delay < -0.25:
                    self.overflows += 1
                    next_t = time.monotonic()
        except Exception as e:
            self._running = False
            self._report_error(f"Reading {os.path.basename(self.path or '')} failed: {e}")
