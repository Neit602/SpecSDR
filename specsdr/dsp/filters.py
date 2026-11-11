"""FIR / IIR building blocks. Every class here keeps its state across calls,
so feeding a signal in arbitrary block sizes gives the same result as one big block."""

from __future__ import annotations

import math

import numpy as np
from numpy.lib.stride_tricks import as_strided
from scipy import signal

FILTER_WINDOWS = {
    "hamming": "Hamming",
    "hann": "Hann",
    "blackman": "Blackman",
    "blackmanharris": "Blackman-Harris",
    "kaiser": "Kaiser (β=8)",
}


def _window_arg(name: str):
    return ("kaiser", 8.0) if name == "kaiser" else name


def kaiser_lowpass(fs: float, f_pass: float, f_stop: float, atten_db: float = 75.0,
                   max_taps: int = 8191) -> np.ndarray:
    """Linear-phase lowpass with passband edge f_pass and stopband edge f_stop (Hz)."""
    if f_stop <= f_pass:
        raise ValueError("f_stop must be above f_pass")
    nyq = fs / 2.0
    f_stop = min(f_stop, nyq)
    width = (f_stop - f_pass) / nyq
    numtaps, beta = signal.kaiserord(atten_db, width)
    numtaps = int(min(max(numtaps, 3), max_taps)) | 1
    cutoff = min((f_pass + f_stop) / 2.0, nyq * 0.999)
    return signal.firwin(numtaps, cutoff, window=("kaiser", beta), fs=fs).astype(np.float32)


def windowed_lowpass(numtaps: int, cutoff: float, fs: float, window: str = "blackmanharris") -> np.ndarray:
    numtaps = max(int(numtaps), 3) | 1
    cutoff = min(max(cutoff, fs * 1e-5), fs / 2 * 0.999)
    return signal.firwin(numtaps, cutoff, window=_window_arg(window), fs=fs).astype(np.float32)


def windowed_bandpass_complex(numtaps: int, f_lo: float, f_hi: float, fs: float,
                              window: str = "blackmanharris") -> np.ndarray:
    """Complex FIR passing [f_lo, f_hi] (Hz, may be negative): a lowpass shifted to the band centre."""
    numtaps = max(int(numtaps), 3) | 1
    centre = (f_lo + f_hi) / 2.0
    half = max(abs(f_hi - f_lo) / 2.0, 1.0)
    h = windowed_lowpass(numtaps, half, fs, window).astype(np.float64)
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    return (h * np.exp(2j * np.pi * centre / fs * n)).astype(np.complex64)


class FirFilter:
    """Stateful FIR filter with optional integer decimation.

    Output k is sum_m h[m] x[n_k - m]; the output grid is anchored to the absolute
    input sample index, so block boundaries never change the result. Implemented as a
    strided view of (history + block) times the reversed taps (BLAS gemv), which was
    measured ~2x faster than scipy.signal.upfirdn for these sizes.
    Accepts 1-D input, or 2-D (n, channels) for real multi-channel audio.
    """

    def __init__(self, taps, decimation: int = 1):
        self.decimation = int(decimation)
        if self.decimation < 1:
            raise ValueError("decimation must be >= 1")
        self._hist: np.ndarray | None = None
        self._phase = 0
        self.set_taps(taps)

    @property
    def taps(self) -> np.ndarray:
        return self._taps

    @property
    def num_taps(self) -> int:
        return len(self._taps)

    def set_taps(self, taps) -> None:
        """Replace coefficients. Input history is kept (re-sized), so no click is produced."""
        taps = np.asarray(taps)
        if taps.ndim != 1 or len(taps) < 1:
            raise ValueError("taps must be a non-empty 1-D array")
        self._taps = taps.astype(np.complex64 if np.iscomplexobj(taps) else np.float32)
        self._rev_cache: dict = {}
        if self._hist is not None:
            need = len(self._taps) - 1
            have = len(self._hist)
            if have > need:
                self._hist = self._hist[have - need:]
            elif have < need:
                pad = np.zeros((need - have,) + self._hist.shape[1:], self._hist.dtype)
                self._hist = np.concatenate([pad, self._hist])

    def reset(self) -> None:
        self._hist = None
        self._phase = 0

    def _rev(self, dtype) -> np.ndarray:
        key = np.dtype(dtype)
        r = self._rev_cache.get(key)
        if r is None:
            r = np.ascontiguousarray(self._taps[::-1]).astype(dtype)
            self._rev_cache[key] = r
        return r

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x)
        h_complex = np.iscomplexobj(self._taps)
        if np.iscomplexobj(x) or h_complex:
            work = np.complex64
        else:
            work = np.float32
        if x.dtype != work:
            x = x.astype(work)
        L = len(self._taps)
        D = self.decimation
        if (self._hist is None or self._hist.dtype != x.dtype
                or self._hist.shape[1:] != x.shape[1:]):
            self._hist = np.zeros((L - 1,) + x.shape[1:], x.dtype)
            self._phase = 0
        z = np.concatenate([self._hist, x]) if L > 1 else x
        p = self._phase
        n_z = len(z)
        nout = (n_z - L - p) // D + 1 if n_z >= L + p else 0
        if nout > 0:
            zz = z[p:]
            if zz.ndim == 1:
                win = as_strided(zz, shape=(nout, L), strides=(D * zz.strides[0], zz.strides[0]),
                                 writeable=False)
                y = win @ self._rev(work)
            else:
                rev = self._rev(work)
                cols = []
                for c in range(zz.shape[1]):
                    col = zz[:, c]
                    win = as_strided(col, shape=(nout, L), strides=(D * col.strides[0], col.strides[0]),
                                     writeable=False)
                    cols.append(win @ rev)
                y = np.stack(cols, axis=1)
            y = y.astype(work, copy=False)
        else:
            y = np.zeros((0,) + x.shape[1:], work)
        nxt = p + nout * D
        self._hist = z[n_z - (L - 1):].copy() if L > 1 else z[:0].copy()
        self._phase = nxt - (n_z - (L - 1))
        return y


class FirInterpolator:
    """Stateful integer interpolator (zero-stuff by K, then FIR). Output length is exactly K*len(x).

    Polyphase form done as one matrix product: (n x M windows of the input) @ (M x K branch
    taps), so the cost is a single BLAS gemm per block whatever K is."""

    def __init__(self, taps, interpolation: int):
        self.K = int(interpolation)
        taps = np.asarray(taps, dtype=np.float64) * self.K
        L = len(taps)
        M = int(math.ceil(L / self.K))
        hp = np.zeros(M * self.K)
        hp[:L] = taps
        # branch k uses taps hp[k::K]; window row holds x[n-M+1 .. n], so reverse the taps
        self._bank = np.ascontiguousarray(hp.reshape(M, self.K)[::-1, :])  # (M, K)
        self._bank_c = self._bank.astype(np.complex64)
        self._bank_f = self._bank.astype(np.float32)
        self.M = M
        self._hist: np.ndarray | None = None

    def reset(self) -> None:
        self._hist = None

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x)
        cplx = np.iscomplexobj(x)
        x = x.astype(np.complex64 if cplx else np.float32, copy=False)
        if self._hist is None or self._hist.dtype != x.dtype:
            self._hist = np.zeros(self.M - 1, x.dtype)
        z = np.concatenate([self._hist, x]) if self.M > 1 else x
        n = len(x)
        if n == 0:
            return np.zeros(0, x.dtype)
        win = as_strided(z, shape=(n, self.M), strides=(z.strides[0], z.strides[0]), writeable=False)
        y = win @ (self._bank_c if cplx else self._bank_f)  # (n, K)
        self._hist = z[len(z) - (self.M - 1):].copy() if self.M > 1 else z[:0].copy()
        return y.reshape(-1).astype(x.dtype, copy=False)


class IirFilter:
    """Stateful IIR (scipy lfilter with carried zi). For low-rate real signals."""

    def __init__(self, b, a):
        self.b = np.asarray(b, dtype=np.float64)
        self.a = np.asarray(a, dtype=np.float64)
        self._zi: np.ndarray | None = None
        self._shape = None

    def reset(self) -> None:
        self._zi = None

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x)
        if len(x) == 0:
            return x
        n_state = max(len(self.a), len(self.b)) - 1
        if self._zi is None or self._shape != x.shape[1:]:
            self._shape = x.shape[1:]
            self._zi = np.zeros((n_state,) + x.shape[1:], np.complex128 if np.iscomplexobj(x) else np.float64)
        if np.iscomplexobj(x) and not np.iscomplexobj(self._zi):
            self._zi = self._zi.astype(np.complex128)
        y, self._zi = signal.lfilter(self.b, self.a, x, axis=0, zi=self._zi)
        return y.astype(x.dtype if x.dtype in (np.float32, np.complex64) else np.float32, copy=False)


def deemphasis(fs: float, tau: float) -> IirFilter:
    """First-order de-emphasis H(s) = 1/(1 + s*tau), bilinear transform pre-warped at 1/(2*pi*tau)."""
    w0 = 1.0 / tau
    # pre-warp so the corner frequency is exact
    wa = 2 * fs * math.tan(w0 / (2 * fs))
    b, a = signal.bilinear([wa], [1.0, wa], fs)
    return IirFilter(b, a)


def dc_blocker(fs: float, corner_hz: float = 30.0) -> IirFilter:
    """y[n] = x[n] - x[n-1] + r*y[n-1]."""
    r = math.exp(-2 * math.pi * corner_hz / fs)
    return IirFilter([1.0, -1.0], [1.0, -r])


# ---------------------------------------------------------------- decimation planning

def _prime_factors(n: int) -> list[int]:
    out, p = [], 2
    while p * p <= n:
        while n % p == 0:
            out.append(p)
            n //= p
        p += 1
    if n > 1:
        out.append(n)
    return out


def choose_decimation(fs_in: float, min_rate: float, max_prime: int = 7) -> int:
    """Largest D with fs_in/D >= min_rate, preferring D whose prime factors are small
    (cheap multistage), giving up at most 25% of decimation for that."""
    d_max = max(1, int(math.floor(fs_in / min_rate + 1e-9)))
    for d in range(d_max, max(1, int(d_max * 0.75)) - 1, -1):
        if max(_prime_factors(d) or [1]) <= max_prime:
            return d
    return d_max


def stage_factors(total: int, max_stage: int = 8) -> list[int]:
    stages: list[int] = []
    for p in sorted(_prime_factors(total), reverse=True):
        for i, s in enumerate(stages):
            if s * p <= max_stage:
                stages[i] = s * p
                break
        else:
            stages.append(p)
    return sorted(stages, reverse=True)


class MultiStageDecimator:
    """Cascade of FirFilter decimators. `protect_hz` is the one-sided band (around DC)
    that must be alias-free at the output; everything above it is only guaranteed to be
    attenuated by later (channel) filtering."""

    def __init__(self, fs_in: float, decimation: int, protect_hz: float, atten_db: float = 80.0):
        self.fs_in = float(fs_in)
        self.decimation = int(decimation)
        self.fs_out = self.fs_in / self.decimation
        self.stages: list[FirFilter] = []
        fs = self.fs_in
        if self.decimation > 1:
            for f in stage_factors(self.decimation):
                fo = fs / f
                f_stop = fo - protect_hz
                taps = kaiser_lowpass(fs, protect_hz, f_stop, atten_db)
                self.stages.append(FirFilter(taps, f))
                fs = fo

    @property
    def total_taps(self) -> int:
        return sum(s.num_taps for s in self.stages)

    def reset(self) -> None:
        for s in self.stages:
            s.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        for s in self.stages:
            x = s.process(x)
        return x
