"""DC (LO leakage) removal for wideband IQ."""

from __future__ import annotations

import math

import numpy as np


class DcRemover:
    """Tracks the DC offset as an exponential average of block means (time constant tau)
    and subtracts it, ramping linearly from the previous to the new estimate across the
    block so there is no step at block boundaries. Notch width ~ 1/(2*pi*tau)."""

    def __init__(self, fs: float, tau: float = 0.1):
        self.fs = float(fs)
        self.tau = float(tau)
        self.dc = np.complex64(0)
        self._primed = False

    def reset(self) -> None:
        self.dc = np.complex64(0)
        self._primed = False

    def process(self, x: np.ndarray) -> np.ndarray:
        n = len(x)
        if n == 0:
            return x
        m = np.complex64(x.mean())
        if not self._primed:
            self.dc = m
            self._primed = True
        k = 1.0 - math.exp(-n / (self.fs * self.tau))
        new = np.complex64(self.dc + k * (m - self.dc))
        if new == self.dc:
            y = x - self.dc
        else:
            ramp = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
            y = x - (self.dc + (new - self.dc) * ramp)
        self.dc = new
        return y.astype(np.complex64, copy=False)
