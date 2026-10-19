"""AGC and squelch.

Both work on fixed-length chunks aligned to the absolute sample count. The gain applied
inside chunk c ramps linearly from the gain at the end of chunk c-1 to the target decided
from chunks up to c-1, so the result never depends on how the stream was split into blocks
and gain changes never produce steps (clicks)."""

from __future__ import annotations

import math

import numpy as np

from .demod import DelayLine


class _ChunkedGain:
    def __init__(self, chunk: int):
        self.chunk = max(1, int(chunk))
        self._pos = 0  # samples already seen in the current chunk
        self._acc = 0.0  # running statistic for the current (incomplete) chunk
        self._gain_start = 1.0  # gain at the start of the current chunk
        self._gain_target = 1.0  # gain reached at the end of the current chunk

    # subclasses: _stat(x) -> per-sample statistic array; _combine(acc, values) ; _decide(stat) -> new target
    def _chunk_stats(self, x: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def _decide(self, stat: float) -> float:
        raise NotImplementedError

    def _reset_stat(self) -> float:
        return 0.0

    def _merge(self, acc: float, vals: np.ndarray) -> float:
        raise NotImplementedError

    def gains(self, x: np.ndarray) -> np.ndarray:
        """Per-sample gain for block x (does not modify x)."""
        n = len(x)
        g = np.empty(n, np.float32)
        stat = self._chunk_stats(x)
        i = 0
        C = self.chunk
        while i < n:
            take = min(C - self._pos, n - i)
            a = self._pos / C
            b = (self._pos + take) / C
            g0 = self._gain_start + (self._gain_target - self._gain_start) * a
            g1 = self._gain_start + (self._gain_target - self._gain_start) * b
            # sample k of the chunk gets the gain at fraction (pos+k+1)/C
            g[i:i + take] = np.linspace(g0, g1, take + 1, dtype=np.float64)[1:]
            self._acc = self._merge(self._acc, stat[i:i + take])
            self._pos += take
            i += take
            if self._pos >= C:
                self._gain_start = self._gain_target
                self._gain_target = self._decide(self._acc)
                self._acc = self._reset_stat()
                self._pos = 0
        return g


class Agc(_ChunkedGain):
    """Peak-envelope AGC.

    threshold_db: input envelope (dBFS) below which gain stops increasing (no noise pumping).
    slope_db: output rise in dB for every 10 dB of input above the threshold (0 = flat).
    attack_ms / decay_ms: envelope time constants. hang_ms: hold before decay starts."""

    TARGET_DB = -6.0
    MAX_GAIN_DB = 100.0
    LIMIT = 0.95

    def __init__(self, fs: float, attack_ms: float = 5.0, decay_ms: float = 300.0,
                 threshold_db: float = -100.0, slope_db: float = 0.0, hang_ms: float = 0.0,
                 chunk_ms: float = 1.0):
        super().__init__(round(fs * chunk_ms / 1000.0))
        self.fs = fs
        self._env = 0.0
        self._last_peak = 0.0
        self._delay = DelayLine(self.chunk)
        self._hang = 0
        self.configure(attack_ms, decay_ms, threshold_db, slope_db, hang_ms)

    def configure(self, attack_ms=None, decay_ms=None, threshold_db=None, slope_db=None, hang_ms=None):
        dt = self.chunk / self.fs
        if attack_ms is not None:
            self.attack_ms = float(attack_ms)
            self._ka = 1.0 - math.exp(-dt / max(self.attack_ms / 1000.0, dt))
        if decay_ms is not None:
            self.decay_ms = float(decay_ms)
            self._kd = 1.0 - math.exp(-dt / max(self.decay_ms / 1000.0, dt))
        if threshold_db is not None:
            self.threshold_db = float(threshold_db)
        if slope_db is not None:
            self.slope_db = float(slope_db)
        if hang_ms is not None:
            self.hang_ms = float(hang_ms)
            self._hang_chunks = int(round(self.hang_ms / 1000.0 / dt))

    def _chunk_stats(self, x):
        a = np.abs(x)
        return a.max(axis=1) if a.ndim == 2 else a

    def _merge(self, acc, vals):
        return max(acc, float(vals.max())) if len(vals) else acc

    def _decide(self, peak):
        self._last_peak = peak
        if peak > self._env:
            self._env += self._ka * (peak - self._env)
            self._hang = self._hang_chunks
        elif self._hang > 0:
            self._hang -= 1
        else:
            self._env += self._kd * (peak - self._env)
        env_db = 20 * math.log10(max(self._env, 1e-12))
        lvl = max(env_db, self.threshold_db)
        out_db = self.TARGET_DB + (lvl - self.threshold_db) * (self.slope_db / 10.0)
        gain_db = min(out_db - lvl, self.MAX_GAIN_DB)
        g = 10 ** (gain_db / 20.0)
        # the audio is delayed by one chunk, so this gain is applied to the very chunk whose
        # peak was just measured: never let it exceed full scale (no onset overshoot)
        if peak > 0:
            g = min(g, self.LIMIT / peak)
        return g

    @property
    def gain_db(self) -> float:
        return 20 * math.log10(max(self._gain_target, 1e-12))

    def process(self, x: np.ndarray) -> np.ndarray:
        """Output is delayed by one chunk (1 ms) so the gain can react to it in time."""
        g = self.gains(x)
        y = self._delay.process(x) * (g[:, None] if x.ndim == 2 else g)
        np.clip(y, -1.0, 1.0, out=y)
        return y


class Squelch(_ChunkedGain):
    """Power squelch on the channel-filtered IQ. Level in dBFS (same scale as the
    spectrum's channel power). Opens above threshold, closes 3 dB below it."""

    HYSTERESIS_DB = 3.0

    def __init__(self, fs: float, threshold_db: float = -60.0, enabled: bool = False, chunk_ms: float = 2.0):
        super().__init__(round(fs * chunk_ms / 1000.0))
        self.threshold_db = threshold_db
        self.enabled = enabled
        self.open = True
        self._level = -150.0
        self._k = 1.0 - math.exp(-chunk_ms / 20.0)  # 20 ms level smoothing
        self._n = 0

    def _reset_stat(self):
        self._n = 0
        return 0.0

    def _chunk_stats(self, x):
        return (x.real * x.real + x.imag * x.imag).astype(np.float64)

    def _merge(self, acc, vals):
        self._n += len(vals)
        return acc + float(vals.sum())

    def _decide(self, energy):
        p = energy / max(self._n, 1)
        db = 10 * math.log10(max(p, 1e-20))
        self._level += self._k * (db - self._level)
        if not self.enabled:
            self.open = True
        elif self.open:
            self.open = self._level > self.threshold_db - self.HYSTERESIS_DB
        else:
            self.open = self._level > self.threshold_db
        return 1.0 if self.open else 0.0

    @property
    def level_db(self) -> float:
        return self._level
