"""Audio output: a jitter buffer between the DSP thread and the sound card callback,
a null sink (paced like a real device, for headless use / tests / no sound library),
and a sounddevice (PortAudio) sink. sounddevice is an optional import."""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

try:  # optional: PortAudio may be missing
    import sounddevice as _sd
    _SD_ERROR = ""
except Exception as e:  # ImportError, OSError (no libportaudio), ...
    _sd = None
    _SD_ERROR = str(e) or e.__class__.__name__


def sounddevice_available() -> tuple[bool, str]:
    return (_sd is not None, _SD_ERROR)


class JitterBuffer:
    """Stereo float32 FIFO.

    - Underrun: what is there is played with a short fade-out, then silence until the
      buffer has refilled to the target (so one hiccup doesn't become a stutter).
    - Overrun: if the fill exceeds `max_fill`, the oldest audio is dropped back down to the
      target, with a short crossfade-free fade-in (counted).
    Thread-safe (one writer, one reader)."""

    FADE = 64

    def __init__(self, sample_rate: float, target_s: float = 0.12, max_s: float = 0.6, capacity_s: float = 2.0):
        self.sample_rate = float(sample_rate)
        self._cap = int(capacity_s * sample_rate)
        self._buf = np.zeros((self._cap, 2), np.float32)
        self._r = 0
        self._fill = 0
        self._lock = threading.Lock()
        self.set_latency(target_s, max_s)
        self._buffering = True
        self._fade_in = False
        self.underruns = 0
        self.overruns = 0
        self.frames_written = 0
        self.frames_played = 0

    def set_latency(self, target_s: float, max_s: float | None = None) -> None:
        self.target = int(target_s * self.sample_rate)
        self.max_fill = int((max_s if max_s else target_s * 5) * self.sample_rate)
        self.max_fill = min(max(self.max_fill, self.target * 2), self._cap)

    @property
    def fill(self) -> int:
        return self._fill

    @property
    def fill_seconds(self) -> float:
        return self._fill / self.sample_rate

    def clear(self) -> None:
        with self._lock:
            self._fill = 0
            self._buffering = True

    def write(self, frames: np.ndarray) -> None:
        if frames.ndim == 1:
            frames = np.repeat(frames[:, None], 2, axis=1)
        frames = frames.astype(np.float32, copy=False)
        n = len(frames)
        if n == 0:
            return
        with self._lock:
            if n >= self._cap:
                frames = frames[-(self._cap - 1):]
                n = len(frames)
            if self._fill + n > self.max_fill:
                drop = self._fill + n - self.target
                drop_old = min(drop, self._fill)
                self._r = (self._r + drop_old) % self._cap
                self._fill -= drop_old
                if drop > drop_old:  # even the new block is too much
                    frames = frames[drop - drop_old:]
                    n = len(frames)
                self.overruns += 1
                self._fade_in = True
            w = (self._r + self._fill) % self._cap
            first = min(n, self._cap - w)
            self._buf[w:w + first] = frames[:first]
            if first < n:
                self._buf[:n - first] = frames[first:]
            self._fill += n
            self.frames_written += n

    def read(self, n: int, out: np.ndarray | None = None) -> np.ndarray:
        if out is None:
            out = np.zeros((n, 2), np.float32)
        with self._lock:
            if self._buffering:
                if self._fill >= self.target:
                    self._buffering = False
                    self._fade_in = True
                else:
                    out[:] = 0
                    return out
            take = min(n, self._fill)
            first = min(take, self._cap - self._r)
            out[:first] = self._buf[self._r:self._r + first]
            if first < take:
                out[first:take] = self._buf[:take - first]
            self._r = (self._r + take) % self._cap
            self._fill -= take
            self.frames_played += take
            if self._fade_in and take:
                f = min(self.FADE, take)
                out[:f] *= np.linspace(0, 1, f, dtype=np.float32)[:, None]
                self._fade_in = False
            if take < n:
                f = min(self.FADE, take)
                if f:
                    out[take - f:take] *= np.linspace(1, 0, f, dtype=np.float32)[:, None]
                out[take:] = 0
                self.underruns += 1
                self._buffering = True
        return out


def volume_to_gain(volume: float) -> float:
    """0..100 slider -> linear gain on a 60 dB range."""
    if volume <= 0:
        return 0.0
    return 10 ** ((min(volume, 100.0) - 100.0) * 0.6 / 20.0)


class AudioSink:
    name = "base"

    def __init__(self, sample_rate: float = 48000.0, latency_s: float = 0.12):
        self.sample_rate = float(sample_rate)
        self.jitter = JitterBuffer(self.sample_rate, latency_s)
        self.gain = volume_to_gain(50)
        self.muted = False
        self.device_underflows = 0  # reported by the driver
        self.running = False

    def set_volume(self, volume: float) -> None:
        self.gain = volume_to_gain(volume)

    def write(self, frames: np.ndarray) -> None:
        self.jitter.write(frames)

    def _fill_output(self, out: np.ndarray) -> None:
        self.jitter.read(len(out), out)
        g = 0.0 if self.muted else self.gain
        if g != 1.0:
            out *= np.float32(g)
        np.clip(out, -1.0, 1.0, out=out)

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    @property
    def underruns(self) -> int:
        return self.jitter.underruns + self.device_underflows

    @property
    def overruns(self) -> int:
        return self.jitter.overruns


class NullSink(AudioSink):
    """Discards audio but consumes it at the real sample rate on its own thread, like a
    sound card would, so buffering / drift correction behave exactly as with a device."""

    name = "null"

    def __init__(self, sample_rate: float = 48000.0, latency_s: float = 0.12, period_s: float = 0.01):
        super().__init__(sample_rate, latency_s)
        self._period = period_s
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_output: np.ndarray | None = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="null-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._thread = None
        self.running = False

    def _run(self) -> None:
        t0 = time.monotonic()
        played = 0
        buf = np.zeros((int(self.sample_rate * self._period * 4) + 16, 2), np.float32)
        while not self._stop.wait(self._period):
            due = int((time.monotonic() - t0) * self.sample_rate) - played
            while due > 0:
                n = min(due, len(buf))
                self._fill_output(buf[:n])
                self.last_output = buf[:n]
                played += n
                due -= n


class SoundDeviceSink(AudioSink):
    name = "sounddevice"
    DEVICE_LATENCY_S = 0.08

    def __init__(self, device=None, sample_rate: float | None = None, latency_s: float = 0.12):
        if _sd is None:
            raise RuntimeError(f"sounddevice / PortAudio not available: {_SD_ERROR}")
        if sample_rate is None:
            info = _sd.query_devices(device, "output")
            sample_rate = float(info["default_samplerate"])
        super().__init__(sample_rate, latency_s)
        self.device = device
        self._stream = None

    @staticmethod
    def output_devices() -> list[tuple[int, str]]:
        if _sd is None:
            return []
        out = []
        try:
            apis = _sd.query_hostapis()
            for i, d in enumerate(_sd.query_devices()):
                if d["max_output_channels"] >= 1:
                    out.append((i, f"{d['name']} ({apis[d['hostapi']]['name']})"))
        except Exception as e:
            log.warning("cannot list audio devices: %s", e)
        return out

    @staticmethod
    def default_output() -> int | None:
        if _sd is None:
            return None
        try:
            d = _sd.default.device
            idx = d[1] if isinstance(d, (list, tuple)) or hasattr(d, "__getitem__") else d
            return int(idx) if idx is not None and int(idx) >= 0 else None
        except Exception:
            return None

    def _callback(self, outdata, frames, time_info, status):
        if status.output_underflow:
            self.device_underflows += 1
        self._fill_output(outdata)

    def start(self) -> None:
        if self.running:
            return
        # A few ms of device buffer ("low") is not enough for a Python callback: any GIL
        # hold-up by the DSP thread then underflows the device although our jitter buffer is
        # full (measured: ~1 underflow / 2 s). 80 ms of device buffer removes that.
        self._stream = _sd.OutputStream(samplerate=self.sample_rate, channels=2, dtype="float32",
                                        device=self.device, callback=self._callback,
                                        latency=self.DEVICE_LATENCY_S)
        self._stream.start()
        self.running = True

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.warning("closing audio stream: %s", e)
        self._stream = None
        self.running = False


def create_sink(device=None, latency_s: float = 0.12, prefer_null: bool = False) -> tuple[AudioSink, str]:
    """Best available sink. Returns (sink, message) - message explains a fallback."""
    if prefer_null or device == "null":
        return NullSink(latency_s=latency_s), ""
    if _sd is None:
        return NullSink(latency_s=latency_s), f"No audio output library ({_SD_ERROR}); using silent null output."
    try:
        sink = SoundDeviceSink(None if device in (None, "", "default") else device, latency_s=latency_s)
        return sink, ""
    except Exception as e:
        return NullSink(latency_s=latency_s), f"Audio device unavailable ({e}); using silent null output."
