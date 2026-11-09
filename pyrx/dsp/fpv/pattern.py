"""FPV test signal: a test pattern of our own, the composite video encoder, and the FM modulator
of a simulated video transmitter. No Qt. Used by the simulated source and by the tests.

The pattern has a frame counter (digits and a machine-readable barcode), colour bars, a greyscale,
resolution wedges and a moving box. The composite signal is built once per colour-frame cycle
(4 frames PAL, 6 NTSC, which also makes the NTSC line period land on whole samples again) as a
template; each output frame is a copy of its template with the moving box and the counter added."""

from __future__ import annotations

import math

import numpy as np

from .standards import PIXEL_US, PIXELS, VideoStandard

COUNTER_BITS = 16
_TEMPLATES: dict = {}   # (standard name, fs) -> composite templates, shared between transmitters

_GLYPHS = {"0": "01110,10001,10011,10101,11001,10001,01110", "1": "00100,01100,00100,00100,00100,00100,01110",
           "2": "01110,10001,00001,00110,01000,10000,11111", "3": "11110,00001,00001,01110,00001,00001,11110",
           "4": "00010,00110,01010,10010,11111,00010,00010", "5": "11111,10000,11110,00001,00001,10001,01110",
           "6": "00110,01000,10000,11110,10001,10001,01110", "7": "11111,00001,00010,00100,01000,01000,01000",
           "8": "01110,10001,10001,01110,10001,10001,01110", "9": "01110,10001,10001,01111,00001,00010,01100"}
BAR_LEVEL = 0.75


def rgb_to_yuv(r, g, b):
    """Gamma-corrected RGB (0..1) -> Y (0..1), U, V with the PAL/NTSC weighting factors."""
    y = 0.299 * r + 0.587 * g + 0.114 * b
    return y, 0.492 * (b - y), 0.877 * (r - y)


def yuv_to_rgb(y, u, v):
    r = y + v / 0.877
    b = y + u / 0.492
    g = (y - 0.299 * r - 0.114 * b) / 0.587
    return r, g, b


class VideoPattern:
    """720-pixel-wide image; static layers as (Y, U, V), dynamic content as luma rectangles."""

    # rows are fractions of the frame height; band tops
    BAND_COUNTER = 0.0
    BAND_BARS = 0.12
    BAND_GREY = 0.45
    BAND_WEDGE = 0.55
    BAND_MOVER = 0.74
    BAND_EDGES = 0.90
    BAR_X = 256             # x of the first barcode cell

    def __init__(self, std: VideoStandard):
        self.std = std
        self.h = std.rows
        self.w = PIXELS
        self.Y = np.zeros((self.h, self.w), np.float32)
        self.U = np.zeros((self.h, self.w), np.float32)
        self.V = np.zeros((self.h, self.w), np.float32)
        self.band = {name: int(getattr(self, "BAND_" + name) * self.h)
                     for name in ("COUNTER", "BARS", "GREY", "WEDGE", "MOVER", "EDGES")}
        self.mover_h = self.band["EDGES"] - self.band["MOVER"] - 8
        self.bar_edges = np.linspace(0, self.w, 9).astype(np.int32)
        self.bar_rgb = BAR_LEVEL * np.array(
            [(1, 1, 1), (1, 1, 0), (0, 1, 1), (0, 1, 0), (1, 0, 1), (1, 0, 0), (0, 0, 1), (0, 0, 0)], np.float32)
        self._draw_static()

    # ----------------------------------------------------------------- static image
    def _draw_static(self):
        b = self.band
        # colour bars (75 %)
        for i in range(8):
            x0, x1 = self.bar_edges[i], self.bar_edges[i + 1]
            y, u, v = rgb_to_yuv(*self.bar_rgb[i])
            self.Y[b["BARS"]:b["GREY"], x0:x1] = y
            self.U[b["BARS"]:b["GREY"], x0:x1] = u
            self.V[b["BARS"]:b["GREY"], x0:x1] = v
        # greyscale, 8 steps
        edges = np.linspace(0, self.w, 9).astype(np.int32)
        for i in range(8):
            self.Y[b["GREY"]:b["WEDGE"], edges[i]:edges[i + 1]] = i / 7.0
        # resolution wedges: sine bursts from 0.5 MHz up to just below the chroma band, each row a little ahead of the last
        wedge = self.Y[b["WEDGE"]:b["MOVER"]]
        n = wedge.shape[0]
        for r in range(n):
            f = (0.5 + (self.wedge_mhz() - 0.5) * np.linspace(0, 1, self.w)) * 1e6
            phase = 2 * np.pi * np.cumsum(f) * (PIXEL_US * 1e-6)
            wedge[r, :] = 0.5 + 0.35 * np.sin(phase + r * 0.12)
        self.Y[b["MOVER"]:b["EDGES"]] = 0.35         # background of the moving box
        # black/white edge pairs at the bottom
        for k, x in enumerate(np.linspace(0, self.w, 13).astype(np.int32)[:-1]):
            if k % 2:
                self.Y[b["EDGES"]:, x:x + self.w // 12] = 1.0
        # the counter band is left black

    def wedge_mhz(self) -> float:
        """Highest wedge frequency: stays out of the colour band, where a notch decoder has no luma."""
        return (self.std.fsc - 1.1e6) / 1e6

    # ----------------------------------------------------------------- dynamic parts
    def box_x(self, frame: int) -> int:
        """Left edge of the moving box (pixels)."""
        travel = self.w - 80
        p = (frame * 4) % (2 * travel)
        return 8 + (p if p < travel else 2 * travel - p)

    def rectangles(self, frame: int) -> list[tuple[int, int, int, int, float]]:
        """Dynamic luma rectangles for this frame: (row0, row1, x0, x1, delta) relative to the
        static background."""
        out = []
        b = self.band
        # moving box
        top = b["MOVER"] + 4
        x = self.box_x(frame)
        out.append((top, top + self.mover_h, x, x + 64, 0.6))
        # barcode: the counter in binary, MSB first, with a start marker cell
        code = frame & ((1 << COUNTER_BITS) - 1)
        y0, y1, cw = self.barcode_rows()
        for k in range(COUNTER_BITS):
            if (code >> (COUNTER_BITS - 1 - k)) & 1:
                out.append((y0, y1, self.BAR_X + k * cw, self.BAR_X + (k + 1) * cw, 1.0))
        # four decimal digits, top left; rows scale with the frame height
        cell = max(4, int(self.h / 576 * 8))
        gy = cell
        for d, ch in enumerate(f"{frame % 10000:04d}"):
            x0 = 48 + d * cell * 6
            for ry, row in enumerate(_GLYPHS[ch].split(",")):
                for rx, c in enumerate(row):
                    if c == "1":
                        out.append((gy + ry * cell, gy + (ry + 1) * cell, x0 + rx * cell, x0 + (rx + 1) * cell, 1.0))
        return out

    def barcode_rows(self) -> tuple[int, int, int]:
        """Barcode (row0, row1, cell width); the counter band of the picture has room for it."""
        h = self.h
        return int(0.06 * h), int(0.10 * h), 20

    def luma(self, frame: int) -> np.ndarray:
        """Full-resolution luma for one frame (0..1), including the dynamic content."""
        y = self.Y.copy()
        for r0, r1, x0, x1, dl in self.rectangles(frame):
            y[r0:r1, x0:x1] = y[r0:r1, x0:x1] + dl
        return y


def read_counter(luma: np.ndarray, pattern: VideoPattern, x_shift: float = 0.0) -> int:
    """Decode the barcode counter from a decoded luma frame (0..1)."""
    y0, y1, cw = pattern.barcode_rows()
    band = luma[y0 + 2:y1 - 2]
    code = 0
    for k in range(COUNTER_BITS):
        x0 = int(round(pattern.BAR_X + k * cw + cw * 0.25 + x_shift))
        x1 = int(round(pattern.BAR_X + (k + 1) * cw - cw * 0.25 + x_shift))
        code = (code << 1) | int(float(band[:, x0:x1].mean()) > 0.5)
    return code


# ----------------------------------------------------------------------------- composite encoder

def _lp(row: np.ndarray, fs_px: float, f_stop: float) -> np.ndarray:
    """Gentle Gaussian low-pass (zero phase) for the band-limited analogue picture signals."""
    sigma = fs_px / (2 * np.pi * f_stop) * 1.2
    half = int(4 * sigma) + 1
    x = np.arange(-half, half + 1)
    k = np.exp(-0.5 * (x / sigma) ** 2)
    k /= k.sum()
    pad = np.pad(row, half, mode="edge")
    return np.convolve(pad, k, mode="valid")


class CompositeEncoder:
    """Composite video frames at sample rate fs as float32 volts (blanking = 0 V)."""

    def __init__(self, std: VideoStandard, fs: float, pattern: VideoPattern | None = None):
        self.std = std
        self.fs = float(fs)
        self.pattern = pattern or VideoPattern(std)
        self.samples_per_line = self.fs / std.line_freq
        self.frame_seconds = std.lines / std.line_freq
        # frames in one full template cycle: chroma repeats after 4 (PAL) / 2 (NTSC) frames
        self.cycle = 4 if std.pal else 6
        self._line_table()
        key = (std.name, self.fs)
        if key not in _TEMPLATES:
            _TEMPLATES[key] = self._build_templates()
        self.templates, self.frame_lengths, self.mean_v = _TEMPLATES[key]

    def _line_table(self):
        s = self.std
        f1, f2 = 23, (336 if s.pal else 286)
        n = s.active_lines
        # frame row -> line number (1-based)
        self.row_line = np.zeros(s.rows, np.int32)
        self.row_line[0::2] = f1 + np.arange(n)
        self.row_line[1::2] = f2 + np.arange(n)
        self.line_row = {int(ln): r for r, ln in enumerate(self.row_line)}

    def _half_kind(self, h: int) -> str:
        """Type of half-line h (0-based, within the frame): 'b' broad, 'e' equalising, '' normal."""
        s = self.std
        if s.pal:
            broad = [(0, 4), (625, 629)]
            eq = [(5, 9), (620, 624), (630, 634), (1245, 1249)]
        else:
            broad = [(6, 11), (531, 536)]
            eq = [(0, 5), (12, 17), (525, 530), (537, 542)]
        for a, b in broad:
            if a <= h <= b:
                return "b"
        for a, b in eq:
            if a <= h <= b:
                return "e"
        return ""

    def _build_templates(self):
        s = self.std
        fs = self.fs
        pat = self.pattern
        spl = self.samples_per_line
        hl = s.line_us * 1e-6 / 2
        # band-limited picture rows, on a 2x oversampled pixel grid
        fs_px = 13.5e6 * 2
        grid = (np.arange(PIXELS * 2) + 0.5) / 2 * PIXEL_US + s.grid_start_us
        n_cycle = self.cycle
        frame_len = fs * s.lines / s.line_freq
        ends = np.round(frame_len * np.arange(n_cycle + 1)).astype(np.int64)
        templates, lengths = [], []
        rise = 0.2e-6
        rows = {}

        def picture_row(r):
            cached = rows.get(r)
            if cached is None:
                y = np.interp(np.arange(PIXELS * 2) / 2, np.arange(PIXELS), pat.Y[r])
                u = np.interp(np.arange(PIXELS * 2) / 2, np.arange(PIXELS), pat.U[r])
                v = np.interp(np.arange(PIXELS * 2) / 2, np.arange(PIXELS), pat.V[r])
                cached = (_lp(y, fs_px, 5.0e6).astype(np.float32),
                          _lp(u, fs_px, 1.3e6).astype(np.float32),
                          _lp(v, fs_px, 1.3e6).astype(np.float32))
                rows[r] = cached
            return cached

        for fidx in range(n_cycle):
            n_frame = int(ends[fidx + 1] - ends[fidx])
            out = np.zeros(n_frame, np.float32)
            t0_samples = ends[fidx]
            for ln0 in range(s.lines):
                ln = ln0 + 1
                t_start = ln0 * spl          # sample index of 0H within this frame (float)
                i0 = int(math.ceil(t_start))
                i1 = min(int(math.ceil(t_start + spl)), n_frame)
                if i1 <= i0:
                    continue
                k = np.arange(i0, i1, dtype=np.float64)
                t = (k - t_start) / fs        # seconds from 0H
                h_a, h_b = 2 * ln0, 2 * ln0 + 1
                sig = np.zeros(len(k), np.float32)

                def pulse(t0, width):
                    # sync pulse with raised-cosine edges
                    a = np.clip((t - t0 + rise / 2) / rise, 0, 1)
                    b = np.clip((t - t0 - width + rise / 2) / rise, 0, 1)
                    return (0.5 - 0.5 * np.cos(np.pi * a)) - (0.5 - 0.5 * np.cos(np.pi * b))

                for part, hh in ((0, h_a), (1, h_b)):
                    kind = self._half_kind(hh)
                    t0 = part * hl
                    if kind == "b":
                        sig += s.sync_v * pulse(t0, hl - 4.7e-6)
                    elif kind == "e":
                        sig += s.sync_v * pulse(t0, s.eq_us * 1e-6)
                    elif part == 0:
                        sig += s.sync_v * pulse(0.0, s.sync_us * 1e-6)
                r = self.line_row.get(ln)
                if r is not None:
                    py, pu, pv = picture_row(r)
                    tu = t * 1e6
                    y = np.interp(tu, grid, py, left=0.0, right=0.0)
                    a0 = np.clip((tu - s.active_start_us + 0.3) / 0.3, 0, 1)
                    a1 = np.clip((s.active_start_us + s.active_us + 0.3 - tu) / 0.3, 0, 1)
                    env = np.minimum(a0, a1) ** 2 * (3 - 2 * np.minimum(a0, a1))
                    sig += (s.setup_v + (s.white_v - s.setup_v) * y).astype(np.float32) * env
                else:
                    env = None
                # colour: phase runs on the sample clock across the whole cycle
                sw = (1.0 if ln0 % 2 == 0 else -1.0) if s.pal else 1.0
                phase = 2 * np.pi * s.fsc * (k + t0_samples) / fs
                sn = np.sin(phase).astype(np.float32)
                cs = np.cos(phase).astype(np.float32)
                if r is not None:
                    uu = np.interp(tu, grid, pu, left=0.0, right=0.0)
                    vv = np.interp(tu, grid, pv, left=0.0, right=0.0)
                    sig += (s.chroma_v * (uu * sn + sw * vv * cs)).astype(np.float32) * env
                # colour burst, except on the field-blanking lines
                if self._half_kind(h_a) == "":
                    bs = s.burst_start_us * 1e-6
                    be = s.burst_cycles / s.fsc
                    g = 0.5 - 0.5 * np.cos(np.pi * np.clip((t - bs) / 0.4e-6, 0, 1))
                    g *= 0.5 - 0.5 * np.cos(np.pi * np.clip((bs + be - t) / 0.4e-6, 0, 1))
                    if s.pal:
                        sig += (s.burst_v * (-0.7071068 * sn + sw * 0.7071068 * cs)).astype(np.float32) * g.astype(np.float32)
                    else:
                        sig += (s.burst_v * (-sn)).astype(np.float32) * g.astype(np.float32)
                out[i0:i1] = sig
            templates.append(out)
            lengths.append(n_frame)
        mean_v = float(np.mean([float(t.mean()) for t in templates]))
        return templates, lengths, mean_v

    # -----------------------------------------------------------------
    def _pixel_pos(self, row: int, x: float) -> int:
        ln = self.row_line[row] - 1
        return int(round(ln * self.samples_per_line + (self.std.grid_start_us + x * PIXEL_US) * self.fs * 1e-6))

    def frame(self, frame_number: int) -> np.ndarray:
        """Composite samples of a frame including the dynamic content."""
        idx = frame_number % self.cycle
        x = self.templates[idx].copy()
        s = self.std
        volts = s.white_v - s.setup_v
        n = len(x)
        for r0, r1, x0, x1, dl in self.pattern.rectangles(frame_number):
            a = dl * volts
            for r in range(r0, min(r1, s.rows)):
                i = self._pixel_pos(r, x0)
                j = self._pixel_pos(r, x1)
                if 0 <= i < j <= n:
                    x[i:j] += a
        return x


class FpvTransmitter:
    """Simulated analog video transmitter: test pattern -> composite -> FM.

    deviation: frequency swing per volt of composite (1 V = sync tip to peak white), Hz/V.
    Modulation is AC-coupled like a real transmitter's video input: the mean frequency of a
    frame sits on the carrier, so `offset` is the transmitter's carrier error."""

    def __init__(self, std: VideoStandard, fs: float, deviation: float = 8e6, offset: float = 0.0,
                 inverted: bool = False, pattern: VideoPattern | None = None, start_frame: int = 0,
                 audio_subcarrier: float = 0.0, audio_tone: float = 1000.0):
        self.std = std
        self.fs = float(fs)
        self.encoder = CompositeEncoder(std, fs, pattern)
        self.deviation = float(deviation)
        self.offset = float(offset)
        self.inverted = bool(inverted)
        self.frame_no = int(start_frame) - 1
        self._buf = np.zeros(0, np.float32)
        self._pos = 0
        self._phase = 0.0
        self._mean: float | None = None
        self.audio_subcarrier = float(audio_subcarrier)   # FM audio subcarrier, Hz (0 = none)
        self.audio_tone = float(audio_tone)
        self._a_phase = 0.0
        self._a_t = 0

    def _next_frame(self) -> None:
        self.frame_no += 1
        self._buf = self.encoder.frame(self.frame_no)
        self._pos = 0
        # the AC coupling of a video input follows the average picture level over a few frames
        m = float(self._buf.mean())
        self._mean = m if self._mean is None else self._mean + 0.3 * (m - self._mean)

    def composite(self, n: int) -> np.ndarray:
        """Next n samples of composite video (volts)."""
        out = np.empty(n, np.float32)
        i = 0
        while i < n:
            if self._pos >= len(self._buf):
                self._next_frame()
            k = min(n - i, len(self._buf) - self._pos)
            out[i:i + k] = self._buf[self._pos:self._pos + k]
            self._pos += k
            i += k
        return out

    def skip(self, n: int) -> None:
        """Advance the timeline without producing samples (retuned out of band)."""
        while n > 0:
            if self._pos >= len(self._buf):
                self._next_frame()
            k = min(n, len(self._buf) - self._pos)
            self._pos += k
            n -= k

    def _audio_sub(self, n: int) -> np.ndarray:
        """Sound on an FM subcarrier (1 kHz tone, +-50 kHz), adding about 0.8 MHz of carrier deviation."""
        t = (np.arange(n) + self._a_t) / self.fs
        self._a_t += n
        beta = 50e3 / self.audio_tone
        ph = 2 * np.pi * self.audio_subcarrier * t + beta * np.sin(2 * np.pi * self.audio_tone * t)
        return (0.8e6 * np.cos(np.mod(ph, 2 * np.pi))).astype(np.float32)

    def generate(self, n: int) -> np.ndarray:
        """Next n unit-power complex samples of the FM signal."""
        c = self.composite(n)
        if n == 0:
            return np.zeros(0, np.complex64)
        dev = self.deviation * (-1.0 if self.inverted else 1.0)
        inst = (c - np.float32(self._mean)) * np.float32(dev) + np.float32(self.offset)
        if self.audio_subcarrier:
            inst = inst + self._audio_sub(n)
        ph = self._phase + 2 * np.pi / self.fs * np.cumsum(inst.astype(np.float64))
        self._phase = float(ph[-1] % (2 * np.pi))
        p32 = np.mod(ph, 2 * np.pi).astype(np.float32)
        out = np.empty(n, np.complex64)
        out.real = np.cos(p32)
        out.imag = np.sin(p32)
        return out
