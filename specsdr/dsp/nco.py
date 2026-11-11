"""Numerically controlled oscillator / mixer with phase continuity across blocks."""

from __future__ import annotations

import numpy as np


class Mixer:
    """Multiplies complex input by exp(j*2*pi*f*n/fs).

    A rotator vector exp(j*w*n) is cached (sliced for shorter blocks) and scaled by the
    running start phase, which is carried in float64 cycles, so every block continues
    exactly where the previous one ended."""

    def __init__(self, fs: float, frequency: float = 0.0):
        self.fs = float(fs)
        self._phase = 0.0  # cycles
        self._freq = 0.0
        self._rot: np.ndarray | None = None
        self.set_frequency(frequency)

    @property
    def frequency(self) -> float:
        return self._freq

    def set_frequency(self, f: float) -> None:
        f = float(f)
        if f != self._freq:
            self._freq = f
            self._rot = None

    def reset(self) -> None:
        self._phase = 0.0

    def _rotator(self, n: int) -> np.ndarray:
        if self._rot is None or len(self._rot) < n:
            length = max(n, 0 if self._rot is None else len(self._rot))
            k = np.arange(length, dtype=np.float64)
            self._rot = np.exp(2j * np.pi * (self._freq / self.fs) * k).astype(np.complex64)
        return self._rot[:n]

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if self._freq == 0.0 or n == 0:
            return np.asarray(x, dtype=np.complex64)
        p0 = np.complex64(np.exp(2j * np.pi * self._phase))
        y = x * self._rotator(n)
        y *= p0
        self._phase = (self._phase + self._freq * n / self.fs) % 1.0
        return y

    def generate(self, n: int) -> np.ndarray:
        """Return the oscillator itself (n samples)."""
        p0 = np.complex64(np.exp(2j * np.pi * self._phase))
        y = self._rotator(n) * p0
        self._phase = (self._phase + self._freq * n / self.fs) % 1.0
        return y
