"""Spectrum analysis: windowed FFT frames in dBFS, and display-side smoothing / peak hold.

dBFS convention: |X[k]|^2 / (sum w)^2, so a complex sinusoid of amplitude 1.0 peaks at
0 dBFS whatever the FFT size or window (the noise floor does depend on both)."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import fft as sfft
from scipy import signal

WINDOWS = {
    "blackmanharris": "Blackman-Harris",
    "nuttall": "Nuttall",
    "blackman": "Blackman",
    "hann": "Hann",
    "hamming": "Hamming",
    "flattop": "Flat top",
    "boxcar": "Rectangular",
}

FFT_SIZES = [1024, 2048, 4096, 8192, 16384, 32768, 65536]


@dataclass
class SpectrumFrame:
    db: np.ndarray            # float32, length fft_size, DC in the middle (fftshifted)
    center: float             # Hz
    sample_rate: float        # Hz
    enbw: float               # equivalent noise bandwidth of the window, in bins
    waterfall: bool = True    # whether this frame should become a waterfall line
    seq: int = 0
    pos: float = 0.0          # where in the processed block this frame ends (0..1)

    @property
    def size(self) -> int:
        return len(self.db)

    def bin_frequencies(self) -> np.ndarray:
        n = len(self.db)
        return self.center + (np.arange(n) - n // 2) * (self.sample_rate / n)

    def frequency_to_bin(self, f: float) -> float:
        n = len(self.db)
        return (f - self.center) / (self.sample_rate / n) + n // 2


def bin_to_frequency(k: float, center: float, sample_rate: float, n: int) -> float:
    return center + (k - n // 2) * sample_rate / n


class SpectrumAnalyzer:
    def __init__(self, fft_size: int = 8192, window: str = "blackmanharris",
                 frame_rate: float = 30.0, averaging: int = 1, waterfall_rate: float | None = None):
        self._hist = np.zeros(0, np.complex64)
        self._since = 0.0
        self._wf_acc = 0.0
        self._seq = 0
        self.configure(fft_size, window, frame_rate, averaging, waterfall_rate)

    def configure(self, fft_size=None, window=None, frame_rate=None, averaging=None, waterfall_rate=None):
        if fft_size is not None:
            self.fft_size = int(fft_size)
        if window is not None:
            self.window_name = window
        if frame_rate is not None:
            self.frame_rate = float(frame_rate)
        if averaging is not None:
            self.averaging = max(1, int(averaging))
        if waterfall_rate is not None or not hasattr(self, "waterfall_rate"):
            self.waterfall_rate = self.frame_rate if waterfall_rate is None else float(waterfall_rate)
        w = signal.get_window(self.window_name, self.fft_size, fftbins=True).astype(np.float64)
        self.enbw = float(len(w) * np.sum(w * w) / np.sum(w) ** 2)
        self._win = (w / np.sum(w)).astype(np.float32)

    @property
    def rate(self) -> float:
        """Frames per second actually produced (enough for both display and waterfall)."""
        return max(self.frame_rate, self.waterfall_rate)

    def reset(self) -> None:
        self._hist = np.zeros(0, np.complex64)
        self._since = 0.0

    def compute(self, x: np.ndarray) -> np.ndarray:
        """dBFS spectrum of the last fft_size*... samples of x (averaging segments, 50% overlap)."""
        n = self.fft_size
        hop = n // 2
        segs = []
        end = len(x)
        for _ in range(self.averaging):
            if end - n < 0:
                break
            segs.append(x[end - n:end])
            end -= hop
        if not segs:
            seg = np.zeros(n, np.complex64)
            seg[n - len(x):] = x
            segs = [seg]
        block = np.stack(segs) * self._win
        spec = sfft.fft(block, axis=1, workers=1)
        p = (spec.real ** 2 + spec.imag ** 2).mean(axis=0)
        p = sfft.fftshift(p)
        return (10.0 * np.log10(p + 1e-20)).astype(np.float32)

    def process(self, x: np.ndarray, center: float, sample_rate: float) -> list[SpectrumFrame]:
        need = self.fft_size + (self.averaging - 1) * (self.fft_size // 2)
        z = np.concatenate([self._hist, x]) if len(self._hist) else x
        hop = sample_rate / self.rate
        frames = []
        base = len(z) - len(x)
        pos = hop - self._since  # samples into x where the next frame is due
        while pos <= len(x):
            p = int(round(base + pos))
            if p > 0:
                self._wf_acc += self.waterfall_rate / self.rate
                wf = self._wf_acc >= 1.0 - 1e-9
                if wf:
                    self._wf_acc -= 1.0
                self._seq += 1
                frames.append(SpectrumFrame(self.compute(z[max(0, p - need):p]), center,
                                            sample_rate, self.enbw, wf, self._seq,
                                            min(1.0, pos / max(len(x), 1))))
            pos += hop
        self._since = len(x) - (pos - hop)
        self._hist = z[-need:].copy() if len(z) > need else z.copy()
        return frames


def noise_floor_db(db: np.ndarray) -> float:
    """Per-bin noise power estimate (dBFS): median of the bins, corrected for the
    median/mean ratio (ln 2) of an exponential distribution."""
    if len(db) == 0:
        return -200.0
    return float(np.median(db) - 10 * math.log10(math.log(2)))


def noise_in_band_db(frame: SpectrumFrame, bandwidth: float) -> float:
    """Noise power (dBFS) that would fall into `bandwidth` Hz, estimated from the frame."""
    per_bin = noise_floor_db(frame.db)
    bin_hz = frame.sample_rate / frame.size
    return per_bin + 10 * math.log10(max(bandwidth / bin_hz / frame.enbw, 1e-12))


class SpectrumDisplay:
    """Display-side state: exponential smoothing and optional peak hold."""

    def __init__(self, smoothing: float = 0.5, peak_hold: bool = False):
        self.smoothing = smoothing  # 0 = none, 0.95 = heavy
        self.peak_hold = peak_hold
        self.current: np.ndarray | None = None
        self.peak: np.ndarray | None = None
        self._key = None

    def reset(self) -> None:
        self.current = None
        self.peak = None

    def update(self, frame: SpectrumFrame) -> np.ndarray:
        key = (frame.size, frame.center, frame.sample_rate)
        if key != self._key or self.current is None:
            self._key = key
            self.current = frame.db.astype(np.float32).copy()
            self.peak = self.current.copy()
        else:
            a = float(self.smoothing)
            self.current = self.current * a + frame.db * (1.0 - a)
            if self.peak_hold:
                np.maximum(self.peak, self.current, out=self.peak)
        if not self.peak_hold:
            self.peak = self.current
        return self.current
