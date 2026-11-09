"""Shared helpers for the FPV video tests: synthesised signals and picture metrics."""

import math

import numpy as np

from pyrx.dsp.filters import kaiser_lowpass
from pyrx.dsp.fpv.decoder import FpvDecoder, FpvParams
from pyrx.dsp.fpv.pattern import FpvTransmitter, read_counter  # noqa: F401
from pyrx.dsp.fpv.standards import NTSC, PAL  # noqa: F401

FS = 20e6


def awgn(n, cnr_db, seed=1):
    """Complex noise whose total power across the full band is `cnr_db` below a unit carrier."""
    rng = np.random.default_rng(seed)
    s = 10 ** (-cnr_db / 20) / math.sqrt(2)
    out = np.empty(n, np.complex64)
    out.real = rng.standard_normal(n, np.float32) * s
    out.imag = rng.standard_normal(n, np.float32) * s
    return out


def make_signal(std, seconds=0.5, cnr=25.0, offset=0.0, inverted=False, deviation=8e6, fs=FS, seed=1):
    tx = FpvTransmitter(std, fs, deviation=deviation, offset=offset, inverted=inverted)
    n = int(seconds * fs)
    x = tx.generate(n)
    if cnr < 90:
        x = x + awgn(n, cnr, seed)
    return x, tx


def decode(x, params=None, fs=FS):
    d = FpvDecoder(fs, params or FpvParams())
    d.process(x)
    d.flush()
    return d, d.take_frames()


def _box_response(f, fs, length):
    return np.abs(np.sinc(f * length / fs) / np.sinc(f / fs))


def decoder_luma_response(rows, std, bw=5.0e6, fs=FS, colour=True):
    """The source as the decoder's video path sees it: its video low-pass, and for colour pictures
    minus the chroma band it takes out (the same two moving averages around fsc). This is the
    "low-pass filtered to the decoder's video bandwidth" reference for the PSNR tests."""
    w = rows.shape[1]
    out = np.empty_like(rows)
    pad = 64
    taps = kaiser_lowpass(fs, bw, min(bw + 1.3e6, fs / 2 * 0.98), 50.0, max_taps=129).astype(np.float64)
    L1 = max(int(round(2 * fs / std.fsc)) | 1, 3)
    L2 = max(int(round(1.6 * fs / std.fsc)) | 1, 3)
    n_fft = 4096
    freqs = np.fft.rfftfreq(n_fft, 1 / 13.5e6)
    h_video = np.abs(np.interp(freqs, np.fft.rfftfreq(8192, 1 / fs), np.abs(np.fft.rfft(taps, 8192))))
    h = h_video.copy()
    if colour:
        c = _box_response(freqs - std.fsc, fs, L1) * _box_response(freqs - std.fsc, fs, L2)
        h = h * (1.0 - np.clip(c, 0.0, 1.0))
    for i, r in enumerate(rows):
        z = np.pad(r, pad, mode="edge").astype(np.float64)
        y = np.fft.irfft(np.fft.rfft(z, n_fft) * h, n_fft)
        out[i] = y[pad:pad + w]
    return out


def luma_psnr(y, reference, margin=(30, 8), edge=16):
    """Aligned PSNR between decoded luma and a reference (both 0..1); returns (psnr, (dx, dy))."""
    y = np.clip(y, 0.0, 1.0)
    top, bottom = margin
    r = reference[top:len(reference) - bottom, edge:-edge]
    best, where = 0.0, (0, 0)
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            d = y[top + dy:y.shape[0] - bottom + dy, edge + dx:y.shape[1] - edge + dx]
            mse = float(np.mean((d - r) ** 2))
            p = 10 * math.log10(1.0 / max(mse, 1e-12))
            if p > best:
                best, where = p, (dx, dy)
    return best, where


def hue_deg(r, g, b):
    return math.degrees(math.atan2(math.sqrt(3) * (g - b), 2 * r - g - b)) % 360.0
