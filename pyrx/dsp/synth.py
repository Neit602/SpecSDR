"""Test-signal synthesis (used by the simulated source and the test-suite)."""

from __future__ import annotations

import math

import numpy as np


def tone(freq: float, fs: float, n: int, amplitude: float = 1.0, start: int = 0) -> np.ndarray:
    t = (np.arange(n) + start) / fs
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def complex_tone(freq: float, fs: float, n: int, amplitude: float = 1.0, start: int = 0) -> np.ndarray:
    t = (np.arange(n) + start) / fs
    return (amplitude * np.exp(2j * np.pi * freq * t)).astype(np.complex64)


def fm_modulate(audio: np.ndarray, fs: float, deviation: float, offset: float = 0.0,
                phase0: float = 0.0) -> np.ndarray:
    """Complex FM: instantaneous frequency = offset + deviation*audio."""
    inst = offset + deviation * np.asarray(audio, np.float64)
    phase = phase0 + 2 * np.pi * np.cumsum(inst) / fs
    return np.exp(1j * phase).astype(np.complex64)


def am_modulate(audio: np.ndarray, fs: float, depth: float, offset: float = 0.0,
                carrier: float = 1.0) -> np.ndarray:
    n = len(audio)
    env = carrier * (1.0 + depth * np.asarray(audio, np.float64))
    return (env * np.exp(2j * np.pi * offset * np.arange(n) / fs)).astype(np.complex64)


def preemphasis(audio: np.ndarray, fs: float, tau: float) -> np.ndarray:
    """Inverse of the receiver de-emphasis (first-order shelf, H(s) = 1 + s*tau, plus a far
    pole for realisability)."""
    from scipy import signal
    wp = 2 * np.pi * 30000.0  # far pole above the audio band
    b, a = signal.bilinear([tau * wp, wp], [1.0, wp], fs)
    return signal.lfilter(b, a, audio).astype(np.float32)


def awgn(n: int, power_db: float, rng: np.random.Generator | None = None) -> np.ndarray:
    rng = rng or np.random.default_rng(0)
    sigma = math.sqrt(10 ** (power_db / 10) / 2)
    return (rng.standard_normal(n, np.float32) * sigma + 1j * rng.standard_normal(n, np.float32) * sigma).astype(np.complex64)
