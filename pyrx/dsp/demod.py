"""Demodulators. Input: complex baseband at the channel rate, already channel-filtered.
Output: float32 audio at the channel rate, shape (n,) mono or (n, 2) stereo.
All state (previous sample, filter memories, oscillator phase) is carried across calls."""

from __future__ import annotations

import math

import numpy as np

from .filters import FirFilter, dc_blocker, deemphasis, kaiser_lowpass
from .nco import Mixer


class Demodulator:
    stereo_output = False

    def process(self, x: np.ndarray) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError

    def reset(self) -> None:
        pass


class FmDiscriminator:
    """Phase difference between consecutive samples, scaled so that a deviation of
    `deviation` Hz gives an output of 1.0."""

    def __init__(self, fs: float, deviation: float):
        self.scale = fs / (2 * math.pi * deviation)
        self._prev = np.complex64(0)

    def reset(self) -> None:
        self._prev = np.complex64(0)

    def process(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return np.zeros(0, np.float32)
        prev = np.empty(len(x), np.complex64)
        prev[0] = self._prev
        prev[1:] = x[:-1]
        self._prev = x[-1]
        d = np.angle(x * np.conj(prev)).astype(np.float32)
        d *= self.scale
        return d


class NfmDemodulator(Demodulator):
    def __init__(self, fs: float, deviation: float = 5000.0):
        self.disc = FmDiscriminator(fs, deviation)
        self.dc = dc_blocker(fs, 20.0)

    def reset(self) -> None:
        self.disc.reset()
        self.dc.reset()

    def process(self, x):
        return self.dc.process(self.disc.process(x)) * np.float32(0.5)


class AmDemodulator(Demodulator):
    """Envelope detector followed by a DC blocker (removes the carrier)."""

    def __init__(self, fs: float):
        self.dc = dc_blocker(fs, 30.0)

    def reset(self) -> None:
        self.dc.reset()

    def process(self, x):
        return self.dc.process(np.abs(x).astype(np.float32))


class SsbDemodulator(Demodulator):
    """USB/LSB/DSB: the sideband selection is done by the (complex) channel filter,
    so the demodulator is just the real part. CW adds a BFO shift first."""

    def __init__(self, fs: float, bfo_hz: float = 0.0):
        self.bfo = Mixer(fs, bfo_hz)

    def set_bfo(self, hz: float) -> None:
        self.bfo.set_frequency(hz)

    def reset(self) -> None:
        self.bfo.reset()

    def process(self, x):
        if self.bfo.frequency:
            x = self.bfo.process(x)
        return np.ascontiguousarray(x.real, dtype=np.float32)


class RawDemodulator(Demodulator):
    """I -> left, Q -> right."""

    stereo_output = True

    def process(self, x):
        out = np.empty((len(x), 2), np.float32)
        out[:, 0] = x.real
        out[:, 1] = x.imag
        return out


class DelayLine:
    """Integer sample delay with state."""

    def __init__(self, delay: int):
        self.delay = int(delay)
        self._buf: np.ndarray | None = None

    def reset(self) -> None:
        self._buf = None

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.delay == 0:
            return x
        if self._buf is None or self._buf.dtype != x.dtype or self._buf.shape[1:] != x.shape[1:]:
            self._buf = np.zeros((self.delay,) + x.shape[1:], x.dtype)
        z = np.concatenate([self._buf, x])
        self._buf = z[len(z) - self.delay:].copy()
        return z[:len(x)]


class WfmDemodulator(Demodulator):
    """Broadcast FM: discriminator, optional stereo decoding, de-emphasis.

    Output is always (n, 2) at the MPX rate and still contains the pilot / subcarrier
    residue; the audio resampler that follows applies the 15 kHz low-pass (it is linear,
    so filtering after the L/R matrix is equivalent and saves two filters).

    Stereo pilot recovery (instead of a feedback PLL, see DECISIONS.md): the MPX is mixed
    down by a 19 kHz oscillator, low-passed to +-100 Hz and decimated, giving the pilot's
    complex envelope u (its phase error vs. the local oscillator). The 38 kHz carrier is
    Re[(osc * u/|u|)^2], with the MPX delayed by the filter group delay so both line up.
    The pilot is detected when the +-100 Hz band holds most of the power within +-2.4 kHz
    of 19 kHz."""

    PILOT = 19000.0

    def __init__(self, fs: float, tau: float = 50e-6, stereo: bool = False, deviation: float = 75000.0):
        self.fs = fs
        self.disc = FmDiscriminator(fs, deviation)
        self.tau = tau
        self.stereo_enabled = stereo
        self.stereo_output = True
        self.pilot_detected = False
        self.pilot_ratio = 0.0
        self._deemph = [deemphasis(fs, tau) if tau else None for _ in range(2)]
        self._osc = Mixer(fs, self.PILOT)
        self._D = max(1, int(fs // 5000))
        fs1 = fs / self._D
        self._stage1 = FirFilter(kaiser_lowpass(fs, 200.0, fs1 - 200.0, 60.0), self._D)
        self._stage2 = FirFilter(kaiser_lowpass(fs1, 100.0, min(600.0, fs1 / 2), 50.0))
        d = (self._stage1.num_taps - 1) / 2 + (self._stage2.num_taps - 1) / 2 * self._D
        self._delay_n = int(round(d))
        self._mpx_delay = DelayLine(self._delay_n)
        self._osc_shift = np.complex64(np.exp(-2j * np.pi * self.PILOT / fs * self._delay_n))
        self._last_u = np.complex64(1.0)
        self._p1 = 0.0
        self._p2 = 0.0

    def set_deemphasis(self, tau: float) -> None:
        if tau != self.tau:
            self.tau = tau
            self._deemph = [deemphasis(self.fs, tau) if tau else None for _ in range(2)]

    def reset(self) -> None:
        self.disc.reset()
        self._osc.reset()
        for f in (self._stage1, self._stage2, self._mpx_delay):
            f.reset()
        for d in self._deemph:
            if d:
                d.reset()

    def process(self, x):
        mpx = self.disc.process(x)
        n = len(mpx)
        out = np.empty((n, 2), np.float32)
        if n == 0:
            return out
        osc = self._osc.generate(n)
        p_phase = self._stage1._phase  # in-block index of the first decimated output
        b1 = self._stage1.process(mpx * np.conj(osc))
        b2 = self._stage2.process(b1)
        if len(b1):
            k = min(1.0, n / (0.3 * self.fs))
            self._p1 += k * (float(np.mean(np.abs(b1) ** 2)) - self._p1)
            self._p2 += k * (float(np.mean(np.abs(b2) ** 2)) - self._p2)
            self.pilot_ratio = self._p2 / max(self._p1 - self._p2, 1e-15)
            thr = 0.5 if self.pilot_detected else 1.0
            self.pilot_detected = self.pilot_ratio > thr and self._p2 > 1e-5
        mpx_d = self._mpx_delay.process(mpx)
        if self.stereo_enabled and self.pilot_detected:
            mag = np.abs(b2)
            u = np.where(mag > 1e-12, b2 / np.maximum(mag, 1e-12), np.complex64(1.0)).astype(np.complex64)
            u_ext = np.concatenate([[self._last_u], u]).astype(np.complex64)
            idx = np.clip((np.arange(n) - p_phase) // self._D + 1, 0, len(u))
            held = u_ext[idx]
            c = osc * self._osc_shift * held
            carrier = (c * c).real.astype(np.float32)
            diff = mpx_d * carrier  # = (L-R)/2 * (1 + 38k/76k terms) ... x2 below
            diff *= np.float32(2.0)
            out[:, 0] = mpx_d + diff
            out[:, 1] = mpx_d - diff
        else:
            out[:, 0] = mpx_d
            out[:, 1] = mpx_d
        if len(b2):
            m = np.abs(b2[-1])
            if m > 1e-12:
                self._last_u = np.complex64(b2[-1] / m)
        d0, d1 = self._deemph
        if d0:
            out[:, 0] = d0.process(out[:, 0])
            out[:, 1] = d1.process(out[:, 1])
        return out


def make_demodulator(mode: str, fs: float, **kw) -> Demodulator:
    mode = mode.upper()
    if mode == "WFM":
        return WfmDemodulator(fs, tau=kw.get("tau", 50e-6), stereo=kw.get("stereo", False))
    if mode == "NFM":
        return NfmDemodulator(fs, kw.get("deviation", 5000.0))
    if mode == "AM":
        return AmDemodulator(fs)
    if mode in ("USB", "LSB", "DSB"):
        return SsbDemodulator(fs, 0.0)
    if mode == "CW":
        return SsbDemodulator(fs, kw.get("cw_pitch", 600.0))
    if mode == "RAW":
        return RawDemodulator()
    raise ValueError(f"unknown mode {mode}")
