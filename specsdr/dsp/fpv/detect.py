"""Scanner detector: is there analog video in this capture?

Detection is by line-rate structure, not by power: a carrier is FM-demodulated and we look for
sync pulses that repeat at the video line period. CW carriers, OFDM-like signals (Wi-Fi,
digital FPV links) and noise have no such structure, so they aren't reported however strong."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import kernels as k
from .standards import NTSC, PAL

MIN_LINES = 100       # consecutive line-spaced pulses required (about 6 ms of video)
MIN_FRACTION = 0.55   # share of the capture's lines that must show a sync pulse


@dataclass
class VideoDetection:
    standard: str
    frequency_offset: float  # carrier relative to the tuned centre, Hz
    level_db: float
    lines: int               # number of line-spaced pulses found
    quality: float           # fraction of the capture's lines with a sync pulse, 0..1
    polarity: int


def _sync_signal(iq: np.ndarray, fs: float):
    z = iq[1:] * np.conj(iq[:-1])
    d = np.arctan2(z.imag, z.real) * np.float32(fs / (2 * np.pi))
    box = int(round(0.8e-6 * fs)) | 1
    c = np.cumsum(np.concatenate([[0.0], d.astype(np.float64)]))
    y = ((c[box:] - c[:-box]) / box).astype(np.float32)
    return d, y


def detect_video(iq: np.ndarray, fs: float) -> VideoDetection | None:
    """Detect analog video in a capture of at least ~15 ms (a few hundred lines)."""
    n_lines = int(len(iq) / (fs / PAL.line_freq))
    if n_lines < MIN_LINES:
        raise ValueError("capture too short for video detection")
    d, y = _sync_signal(iq, fs)
    best = None
    min_w, max_w = int(3.4e-6 * fs), int(6.8e-6 * fs)
    edge = int(0.5e-3 * fs)
    out = np.zeros(4096)
    for pol in (1, -1):
        yy = y if pol > 0 else -y
        q = np.sort(yy[::32])
        m = float(q[max(1, int(0.005 * len(q)))])
        med = float(q[len(q) // 2])
        th = m + 0.35 * (med - m)
        cnt, _ = k.scan_pulses(yy, 0, 1, len(yy) - edge, th, min_w, max_w, out)
        if cnt < MIN_LINES:
            continue
        t = out[:cnt]
        dt = np.diff(t)
        p_pal, p_ntsc = fs / PAL.line_freq, fs / NTSC.line_freq
        # periods within 0.5 % of a video line period (half-line pulses are excluded by that)
        ok = (np.abs(dt - p_pal) < 0.005 * p_pal) | (np.abs(dt - p_ntsc) < 0.005 * p_ntsc)
        n_ok = int(np.count_nonzero(ok))
        expected = len(yy) / ((p_pal + p_ntsc) / 2)
        frac = n_ok / max(expected, 1.0)
        if n_ok >= MIN_LINES and frac >= MIN_FRACTION:
            period = float(np.median(dt[ok]))
            std = "PAL" if abs(period - p_pal) < abs(period - p_ntsc) else "NTSC"
            cand = (frac, n_ok, std, pol)
            if best is None or cand > best:
                best = cand
    if best is None:
        return None
    frac, n_ok, std, pol = best
    level = 10 * np.log10(float(np.mean(iq.real ** 2 + iq.imag ** 2)) + 1e-20)
    return VideoDetection(std, float(np.mean(d)), float(level), n_ok, float(min(frac, 1.0)), pol)
