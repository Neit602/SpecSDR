"""Audio-rate resampling: integer pre-decimation + arbitrary-ratio polyphase resampler.

The arbitrary resampler's ratio can be nudged while running (clock-drift correction
between the SDR and the sound card) without discontinuities."""

from __future__ import annotations

import math

import numpy as np

from .filters import FirFilter, kaiser_lowpass


class ArbResampler:
    """Polyphase resampler with linear interpolation between `phases` sub-filters.

    Works on 1-D (mono / complex) or 2-D (n, channels) input."""

    def __init__(self, fs_in: float, fs_out: float, cutoff: float | None = None,
                 phases: int = 64, atten_db: float = 70.0, stop: float | None = None):
        self.fs_in = float(fs_in)
        self.fs_out_nominal = float(fs_out)
        lo = min(self.fs_in, self.fs_out_nominal)
        fp = 0.45 * lo if cutoff is None else min(cutoff, 0.45 * lo)
        f_stop = lo - fp if stop is None else min(stop, lo - fp)
        f_stop = max(f_stop, fp * 1.05)
        P = int(phases)
        proto = kaiser_lowpass(self.fs_in * P, fp, f_stop, atten_db, max_taps=P * 512 - 1).astype(np.float64)
        T = int(math.ceil(len(proto) / P))
        h = np.zeros(P * T + 1)
        h[:len(proto)] = proto
        h *= P / proto.sum()
        self.P, self.T = P, T
        # table[p, k] = h[k*P + p], p = 0..P (row P == row 0 shifted by one tap)
        idx = np.arange(T)[None, :] * P + np.arange(P + 1)[:, None]
        self._table = h[idx].astype(np.float32)
        self.cutoff = fp
        self._hist: np.ndarray | None = None
        self._t = float(T - 1)
        self.set_output_rate(fs_out)

    @property
    def delay_out_samples(self) -> float:
        return (self.T - 1) / 2.0 * self.fs_out / self.fs_in

    def set_output_rate(self, fs_out: float) -> None:
        self.fs_out = float(fs_out)
        self.step = self.fs_in / self.fs_out

    def reset(self) -> None:
        self._hist = None
        self._t = float(self.T - 1)

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x)
        if x.dtype not in (np.float32, np.complex64):
            x = x.astype(np.complex64 if np.iscomplexobj(x) else np.float32)
        T = self.T
        if self._hist is None or self._hist.dtype != x.dtype or self._hist.shape[1:] != x.shape[1:]:
            self._hist = np.zeros((T - 1,) + x.shape[1:], x.dtype)
            self._t = float(T - 1)
        z = np.concatenate([self._hist, x])
        nz = len(z)
        t = self._t
        if nz > t:
            n_out = int(math.ceil((nz - t) / self.step))
            pos = t + np.arange(n_out, dtype=np.float64) * self.step
            while n_out > 0 and pos[n_out - 1] >= nz:
                n_out -= 1
            pos = pos[:n_out]
        else:
            n_out = 0
            pos = np.zeros(0)
        if n_out:
            i = np.floor(pos).astype(np.int64)
            fp = (pos - i) * self.P
            p = np.minimum(fp.astype(np.int64), self.P - 1)
            a = (fp - p).astype(np.float32)[:, None]
            coef = self._table[p] * (1.0 - a) + self._table[p + 1] * a  # (n, T)
            gather = z[i[:, None] - np.arange(T)[None, :]]  # (n, T[, C])
            if gather.ndim == 2:
                y = np.einsum("nk,nk->n", gather, coef.astype(gather.dtype, copy=False))
            else:
                y = np.einsum("nkc,nk->nc", gather, coef.astype(gather.real.dtype, copy=False))
            y = y.astype(x.dtype, copy=False)
        else:
            y = np.zeros((0,) + x.shape[1:], x.dtype)
        self._t = t + n_out * self.step - (nz - (T - 1))
        self._hist = z[nz - (T - 1):].copy() if T > 1 else z[:0].copy()
        return y


class AudioResampler:
    """fs_in -> fs_out with a lowpass at `cutoff`.

    For large ratios an integer FIR decimator does the bulk of the work (and the sharp
    audio lowpass), then the arbitrary resampler finishes the job."""

    def __init__(self, fs_in: float, fs_out: float, cutoff: float | None = None,
                 stop: float | None = None):
        self.fs_in = float(fs_in)
        self.fs_out = float(fs_out)
        cutoff = 0.45 * min(fs_in, fs_out) if cutoff is None else min(cutoff, 0.45 * min(fs_in, fs_out))
        self.cutoff = cutoff
        d = int(math.floor(self.fs_in / self.fs_out))
        self.pre: FirFilter | None = None
        fs_mid = self.fs_in
        if d >= 2:
            fs_mid = self.fs_in / d
            # the ArbResampler stage will remove everything above fs_out - cutoff;
            # this stage only needs to stop what would alias below that.
            f_stop = fs_mid - cutoff if stop is None else min(stop, fs_mid - cutoff)
            f_stop = max(f_stop, cutoff * 1.05)
            self.pre = FirFilter(kaiser_lowpass(self.fs_in, cutoff, f_stop, 70.0), d)
            stop = None  # the sharp edge is done; the last stage only needs anti-aliasing
        self.fs_mid = fs_mid
        self.arb = ArbResampler(fs_mid, fs_out, cutoff, stop=stop)

    def set_drift(self, ratio: float) -> None:
        """ratio > 1 produces more output samples per input (sound card is faster)."""
        self.arb.set_output_rate(self.fs_out * ratio)

    def reset(self) -> None:
        if self.pre:
            self.pre.reset()
        self.arb.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.pre is not None:
            x = self.pre.process(x)
        return self.arb.process(x)
