"""Analog FPV video decoder (no Qt, no threads): FM video IQ in, video frames out.

    IQ -> DC removal -> AFC mixer -> FM discriminator -> video low-pass -> sync separation
       -> horizontal PLL + vertical sync -> black-level clamp / AGC -> line resampling + colour
       -> field assembly -> deinterlace -> RGB frames

Everything runs on a fixed grid of chunks anchored to the sample stream, so any way of
cutting the input into blocks gives exactly the same frames. Only `notify_gap` (samples lost
upstream) is outside the stream, by nature."""

from __future__ import annotations

import collections
import concurrent.futures
import logging
import math
import weakref
from dataclasses import dataclass

import numpy as np

from ..dc import DcRemover
from ..filters import kaiser_lowpass
from ..nco import Mixer
from . import kernels as k
from .standards import NTSC, PAL, PIXEL_US, PIXELS, STANDARDS, VideoStandard

log = logging.getLogger(__name__)


@dataclass
class FpvParams:
    standard: str = "auto"          # auto / PAL / NTSC
    video_bw: float = 5.0e6
    deinterlace: str = "weave"      # weave / double
    colour: bool = True
    saturation: float = 1.0
    brightness: float = 0.0         # -0.5 .. 0.5 (added after contrast)
    contrast: float = 1.0
    agc: bool = True
    gain_db: float = 0.0            # manual gain (AGC off), relative to nominal deviation
    sync_threshold: float = 0.5     # slicing point between sync tip and blanking
    h_shift_us: float = 0.0
    v_shift: int = 0                # rows of the frame
    show_raw: bool = False          # keep drawing the (noise) picture when unlocked


@dataclass
class FpvStats:
    h_locked: bool = False
    v_locked: bool = False
    colour_locked: bool = False
    standard: str = "?"
    polarity: int = 0
    carrier_offset: float = 0.0     # measured transmitter offset, Hz
    level_db: float = -150.0        # IQ power, dBFS
    cnr_db: float = 0.0             # estimated carrier-to-noise in the sample rate bandwidth
    fps: float = 0.0
    fields: int = 0                 # completed fields
    frames: int = 0                 # frames delivered
    dropped_fields: int = 0
    dropped_samples: int = 0
    lines_total: int = 0
    lines_lost: int = 0             # lines without a detected sync edge, while H-locked
    relocks: int = 0
    afc_hz: float = 0.0             # AFC correction applied
    latency_ms: float = 0.0


@dataclass
class VideoFrame:
    rgb: np.ndarray
    y: np.ndarray
    u: np.ndarray
    v: np.ndarray
    standard: str
    number: int                     # frame index
    locked: bool
    colour: bool
    t_stream: int                   # stream sample position where the frame was completed
    arrival: float = 0.0            # monotonic time of the block that completed it


@dataclass
class FieldInfo:
    start: int                      # stream position of the vertical sync
    parity: int
    clean: bool
    standard: str
    lines_lost: int
    rows_filled: int


class FpvDecoder:
    CHUNK_S = 6.5536e-3             # 131072 samples at 20 MS/s: one HackRF USB transfer
    MAX_GAP_FIELDS = 10.0           # longer gaps restart the decoder

    def __init__(self, fs: float, params: FpvParams | None = None, keep_frames: int = 16,
                 audio=None):
        self.fs = float(fs)
        self.params = params or FpvParams()
        self.audio = audio
        self.stats = FpvStats()
        self.frames: collections.deque[VideoFrame] = collections.deque(maxlen=keep_frames)
        self.field_log: collections.deque[FieldInfo] = collections.deque(maxlen=512)
        self.chunk = int(round(fs * self.CHUNK_S))
        self._box = int(round(0.8e-6 * fs)) | 1
        self._pending_arrivals: collections.deque = collections.deque()
        self._front_q: collections.deque = collections.deque()
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fpv-front")
        weakref.finalize(self, self._pool.shutdown, wait=False)
        self.on_frame = None            # optional callback(VideoFrame), called from process()
        self._build()

    # -------------------------------------------------------------------- construction
    def _build(self) -> None:
        fs = self.fs
        self.std = STANDARDS.get(self.params.standard, PAL)
        self._dc = DcRemover(fs, tau=0.05)
        self._afc_mix = Mixer(fs, 0.0)
        self._afc = 0.0                  # NCO correction, Hz (the signal is shifted by -afc)
        self._afc_acc: collections.deque = collections.deque()
        self._afc_primed = False
        self._afc_chunks = 0
        self._hit_lines = 0               # sync-failure watchdog, see _sync_failing
        self._hit_count = 0
        self._bad_windows = 0
        self._prev = np.complex64(0)
        self._set_video_filter(self.params.video_bw)
        self._reset_inbuf()
        # sample buffers: H history + one chunk
        self._H = int(fs * 150e-6)
        N = self._H + self.chunk
        self._v = np.zeros(N, np.float32)
        self._y = np.zeros(N, np.float32)
        self._n0 = -self._H               # absolute stream index of buffer element 0
        self._box_hist = np.zeros(self._box - 1, np.float32)
        self._stream = 0                  # samples consumed
        # sync
        self._pol = 0
        self._pending_pol = 0
        self.st = np.zeros(k.ST_LEN)
        self.st[k.ST_THR] = self.params.sync_threshold
        self._pulses = {+1: collections.deque(maxlen=32), -1: collections.deque(maxlen=32)}
        self._scan = {+1: 1, -1: 1}
        self._hits_ewma = 0.0
        self._last_lock_state = False
        self._lines = 0                  # running line number
        n_max = int(self.chunk / (fs / 16000)) + 6
        self._b_lines = np.zeros(n_max)
        self._b_hits = np.zeros(n_max, np.bool_)
        self._b_flags = np.zeros(n_max, np.int32)
        self._b_tips = np.zeros(n_max, np.float64)
        self._b_blanks = np.zeros(n_max, np.float64)
        self._ev_half = np.zeros(k.EV_MAX, np.int64)
        # vertical
        self.v_anchor: int | None = None  # line number of the current field's broad start
        self.v_parity = 0
        self._last_vsync_h: int | None = None  # half-line counter at the last vsync
        self._vcount = 0                 # half-lines since start
        self._v_miss = 0
        self._delta = 0
        self._fields_since_lock = 0
        self._no_v_since_h = 0.0
        self._cur: dict | None = None
        self._spare_field = None
        self._frame_no = 0
        self._weave: dict | None = None
        self._free_next = None
        self._free_row = 0
        self._free_field = None
        # colour
        self.cs = np.zeros(k.CS_LEN)
        self._colour = False
        self._colour_hold = 0
        self._prev_uv = np.zeros((2, PIXELS), np.float32)
        self._lvl = 0.0
        self._m4 = 0.0
        self.counters = collections.Counter()

    def _reset_inbuf(self):
        self._inbuf = np.empty(self.chunk, np.complex64)
        self._infill = 0

    def _set_video_filter(self, bw: float) -> None:
        bw = float(min(max(bw, 1.0e6), min(self.fs / 2 * 0.8, 8e6)))
        stop = min(bw + 1.3e6, self.fs / 2 * 0.98)
        taps = kaiser_lowpass(self.fs, bw, stop, 50.0, max_taps=129).astype(np.float32)
        self._v_taps = taps
        self._v_hist = np.zeros(len(taps) - 1, np.float32)

    # -------------------------------------------------------------------- public API
    def set_params(self, **kw) -> None:
        """Change settings. Safe between process() calls; everything takes effect immediately."""
        p = self.params
        rebuild_filter = False
        for key, value in kw.items():
            if key == "video_bw" and value != p.video_bw:
                rebuild_filter = True
            setattr(p, key, value)
        self.st[k.ST_THR] = p.sync_threshold
        if rebuild_filter:
            self._set_video_filter(p.video_bw)
        if "standard" in kw and kw["standard"] != "auto":
            self.std = STANDARDS[kw["standard"]]

    def process(self, iq: np.ndarray, arrival: float | None = None) -> None:
        """Feed samples (any block size). Frames accumulate in self.frames / on_frame."""
        if arrival is not None:
            self._pending_arrivals.append((self._stream + self._infill + len(iq), arrival))
            while len(self._pending_arrivals) > 256:
                self._pending_arrivals.popleft()
        i = 0
        n = len(iq)
        self._drain_front(keep=True)
        while i < n:
            if self._infill == 0 and n - i >= self.chunk:
                self._chunk(iq[i:i + self.chunk])
                i += self.chunk
                continue
            take = min(n - i, self.chunk - self._infill)
            self._inbuf[self._infill:self._infill + take] = iq[i:i + take]
            self._infill += take
            i += take
            if self._infill == self.chunk:
                self._infill = 0
                self._chunk(self._inbuf)
        self._drain_front(keep=True)

    def _drain_front(self, keep: bool) -> None:
        """Consume finished chunks. keep=True leaves one running to overlap with the next."""
        q = self._front_q
        while len(q) > (1 if keep else 0):
            self._back(*q.popleft().result())

    def notify_gap(self, n: int) -> None:
        """`n` samples were lost upstream (USB overflow, or we shed load). Time is kept by
        replaying the gap as held samples; a long gap restarts acquisition."""
        n = int(n)
        if n <= 0:
            return
        self.flush()
        self.stats.dropped_samples += n
        if n > self.MAX_GAP_FIELDS * self.fs / self.std.field_rate:
            self._restart_after_gap(n)
            return
        if self._cur is not None:
            self._cur["dirty"] = True
        self._infill = 0                   # samples before the gap can't continue a chunk
        remaining = n
        while remaining > 0:
            take = min(remaining, self.chunk)
            self._hold_chunk(take)
            remaining -= take
        self.counters["gaps"] += 1

    def _restart_after_gap(self, n: int) -> None:
        self._infill = 0
        self._stream += n
        self._n0 += n
        self.st[k.ST_MODE] = 0
        self._scan = {+1: 1, -1: 1}
        for q in self._pulses.values():
            q.clear()
        self._abandon_field()
        self._last_vsync_h = None
        self.counters["restarts"] += 1

    def _hold_chunk(self, n: int) -> None:
        last = float(self._y[-1])
        lastv = float(self._v[-1])
        self._shift_in(np.full(n, lastv, np.float32), np.full(n, last, np.float32))
        self._stream += n
        self._track()

    def close(self) -> None:
        """Release the front-end thread (idle decoders are also collected with the object)."""
        self._pool.shutdown(wait=False)

    @staticmethod
    def warmup(fs: float = 20e6) -> None:
        """Run the whole chain once on a short synthetic signal, so numba's first-call compile
        (or cache load) happens before real samples arrive."""
        from .pattern import FpvTransmitter
        dec = FpvDecoder(fs)
        tx = FpvTransmitter(PAL, fs)
        dec.process(tx.generate(int(fs * 0.052)))
        dec.flush()

    # -------------------------------------------------------------------- chunk processing
    def _shift_in(self, v_new: np.ndarray, y_new: np.ndarray) -> None:
        n = len(v_new)
        vb, yb = self._v, self._y
        N = len(vb)
        if n < N:
            vb[:N - n] = vb[n:]
            yb[:N - n] = yb[n:]
            vb[N - n:] = v_new
            yb[N - n:] = y_new
        else:
            vb[:] = v_new[-N:]
            yb[:] = y_new[-N:]
        self._n0 += n

    def _chunk(self, x: np.ndarray) -> None:
        """Fixed-size chunk. The front end (demodulation and filtering) runs on a worker thread
        one chunk ahead of the back end (sync and picture), and the two are always consumed in
        order, so the result does not depend on the threads. numpy and the numba kernels release
        the GIL, so this really is parallel (see DECISIONS.md)."""
        self._front_q.append(self._pool.submit(self._front, x.copy()))
        self._stream += len(x)
        if len(self._front_q) > 1:
            self._back(*self._front_q.popleft().result())

    def _front(self, x: np.ndarray):
        """FM demodulation. Its state is touched by this function only."""
        x = self._dc.process(x)
        p = x.real ** 2 + x.imag ** 2
        lvl = float(np.mean(p))
        m4 = float(np.mean(p * p))
        if self._afc != 0.0:
            x = self._afc_mix.process(x)
        z = np.empty(len(x), np.complex64)
        z[0] = x[0] * np.conj(self._prev)
        z[1:] = x[1:] * np.conj(x[:-1])
        self._prev = x[-1]
        d = np.arctan2(z.imag, z.real) * np.float32(self.fs / (2 * math.pi))
        d = d.astype(np.float32, copy=False)
        if self.audio is not None:
            self.audio.process(d)
        v = np.empty(len(d), np.float32)
        k.fir_filter(d, self._v_hist, self._v_taps, v)
        self._update_afc(float(np.mean(d)))
        box = self._box
        cc = np.concatenate([self._box_hist, v])
        c = np.cumsum(cc, dtype=np.float64)
        y = ((c[box - 1:] - np.concatenate([[0.0], c[:len(cc) - box]])) / box).astype(np.float32)
        self._box_hist = cc[-(box - 1):].copy()
        return v, y, lvl, m4

    def _back(self, v: np.ndarray, y: np.ndarray, lvl: float, m4: float) -> None:
        """Sync and picture for one chunk of (v, y)."""
        self._lvl += 0.1 * (lvl - self._lvl)
        self._m4 += 0.1 * (m4 - self._m4)
        if self._pol < 0:
            v = -v
            y = -y
        self._shift_in(v, y)
        self._track()
        self._update_signal_stats()

    def flush(self) -> None:
        """Finish all work submitted so far (frames from the chunks already fed appear now)."""
        while self._front_q:
            self._back(*self._front_q.popleft().result())

    def _update_signal_stats(self) -> None:
        st = self.stats
        st.level_db = 10 * math.log10(max(self._lvl, 1e-15))
        # M2M4 estimator: the envelope of a constant-amplitude FM carrier only varies through the
        # noise, so C/N follows from the 2nd and 4th moments of |x| (in the sample-rate bandwidth)
        c2 = 2 * self._lvl ** 2 - self._m4
        cn = math.sqrt(max(c2, 0.0))
        n = self._lvl - cn
        st.cnr_db = float(min(max(10 * math.log10(max(cn, 1e-12) / max(n, 1e-12)), -5.0), 60.0))

    def _update_afc(self, m: float) -> None:
        """Slow AFC. The running mean of the discriminator over one field is the residual
        carrier offset (the video is AC-coupled in the transmitter). Updated every ~10 ms."""
        dq = self._afc_acc
        dq.append(m)
        n_field = max(1, int(round(self.fs / PAL.field_rate / self.chunk)))
        if len(dq) > n_field:
            dq.popleft()
        self._afc_chunks += 1
        if len(dq) < n_field:
            self.stats.carrier_offset = self._afc + float(np.mean(dq))
            return
        res = float(np.mean(dq))
        self.stats.carrier_offset = self._afc + res
        if self._afc_chunks % 6 == 0:
            f = self._afc + 0.3 * res
            f = min(max(f, -6e6), 6e6)
            self._afc = f
            self._afc_mix.set_frequency(-f)
        self.stats.afc_hz = self._afc

    # -------------------------------------------------------------------- synchronisation
    def _period_limits(self) -> tuple[float, float]:
        """Search range for the line period: both standards unless one is forced."""
        p = self.params.standard
        lo = self.fs / NTSC.line_freq * 0.995 if p != "PAL" else self.fs / PAL.line_freq * 0.995
        hi = self.fs / PAL.line_freq * 1.005 if p != "NTSC" else self.fs / NTSC.line_freq * 1.005
        return lo, hi

    def _acquire(self) -> None:
        """Pulse-train search: finds sync pulses (either polarity while unknown) and hands over
        to the line tracker once a run of them is spaced like video lines."""
        fs = self.fs
        n0 = self._n0
        N = len(self._y)
        y = self._y
        out = np.zeros(k.MAX_PULSES * 2)
        lo, hi = self._period_limits()
        min_w, max_w = int(3.4e-6 * fs), int(6.8e-6 * fs)
        for pol in ((self._pol,) if self._pol else (+1, -1)):
            yy = y if pol > 0 else -y
            recent = yy[N - self.chunk:]
            q = np.sort(recent[::16])
            m = float(q[max(1, int(0.005 * len(q)))])
            med = float(q[len(q) // 2])
            hi_lvl = float(q[int(0.8 * len(q))])
            th = m + 0.35 * (med - m)
            start = max(self._scan[pol] - n0, 1)
            if start >= N - int(8e-6 * fs):
                continue
            yy = np.ascontiguousarray(yy)
            cnt, nxt = k.scan_pulses(yy, n0, start, N - int(10e-6 * fs), th, min_w, max_w, out)
            self._scan[pol] = n0 + int(nxt)
            q_pulses = self._pulses[pol]
            for i in range(cnt):
                q_pulses.append(float(out[i]))
            lk = self._find_train(q_pulses, lo, hi)
            if lk is not None:
                period, last = lk
                # make sure pulse levels look sensible relative to the line period: lock
                self._lock_h(pol, period, last, m, med, hi_lvl)
                return

    def _find_train(self, q, lo, hi):
        need = 8
        n = len(q)
        if n < need:
            return None
        ts = np.array(list(q)[-need:])
        d = np.diff(ts)
        if np.any(d < lo) or np.any(d > hi):
            return None
        if float(d.max() - d.min()) > 0.01 * (lo + hi) / 2:
            return None
        return float(d.mean()), float(ts[-1])

    def _lock_h(self, pol, period, last, tip, med, hi_lvl):
        st = self.st
        p = self._pol
        if p != pol:
            # adopt the polarity; everything in the buffers is flipped
            if pol < 0:
                self._v = -self._v
                self._y = -self._y
                self._box_hist = -self._box_hist
            self._pol = pol
            self.stats.polarity = pol
        tip, blank = self._levels_from_pulses(last, period, tip, med)
        st[k.ST_MODE] = 1
        st[k.ST_PERIOD] = period
        st[k.ST_NEXT] = last + period
        st[k.ST_TIP] = tip
        st[k.ST_BLANK] = blank
        st[k.ST_MISS] = 0
        for q in self._pulses.values():     # spent: a later re-phase search must not reuse them
            q.clear()
        self._hits_ewma = 1.0
        # the line period already tells PAL from NTSC (0.7 % apart)
        self._set_standard(self._period_std(period))
        self.counters["locks"] += 1

    def _levels_from_pulses(self, last, period, tip0, med):
        """Sync tip and blanking level measured on the pulses just found (not statistics of the
        whole picture, which can be far from the blanking level)."""
        fs = self.fs
        n0 = self._n0
        y = self._y
        tips, blanks = [], []
        cl_a, cl_b = int(self.std.clamp_us[0] * 1e-6 * fs), int(self.std.clamp_us[1] * 1e-6 * fs)
        for i in range(16):
            t = last - i * period
            j = int(round(t - n0))
            if j - 4 < 0 or j + cl_b + 2 >= len(y):
                continue
            tips.append(float(np.mean(y[j + int(1.0e-6 * fs):j + int(3.6e-6 * fs)])))
            blanks.append(float(np.mean(y[j + cl_a:j + cl_b])))
        if not tips:
            return float(tip0), float(med)
        return float(np.median(tips)), float(np.median(blanks))

    def _period_std(self, period: float) -> str:
        return "PAL" if abs(period - self.fs / PAL.line_freq) < abs(period - self.fs / NTSC.line_freq) else "NTSC"

    def _set_standard(self, name: str) -> None:
        if self.params.standard != "auto":
            name = self.params.standard
        std = STANDARDS[name]
        if std is not self.std:
            self.std = std
            self._weave = None
        self.stats.standard = name

    def _track(self) -> None:
        st = self.st
        y = self._y
        n0 = self._n0
        if st[k.ST_MODE] == 0:
            self._acquire()
            self._no_v_since_h = 0.0
            if st[k.ST_MODE] == 0:
                self.stats.h_locked = False
                self.stats.v_locked = False
                if self.params.show_raw:
                    self._free_run()
                return
        fs = self.fs
        n_lines = k.track_lines(y, n0, st, int(2.0e-6 * fs), int(4.7e-6 * fs),
                                int(1.0e-6 * fs), int(3.6e-6 * fs), int(self.std.clamp_us[0] * 1e-6 * fs),
                                int(self.std.clamp_us[1] * 1e-6 * fs), 0.12, 0.004, 0.05,
                                self._b_lines, self._b_hits, self._b_flags, self._b_tips, self._b_blanks)
        if st[k.ST_MODE] == 0:
            log.warning("FPV line tracker fell behind its buffer (next %.0f, buffer start %d); re-acquiring",
                        st[k.ST_NEXT], n0)
            self.counters["grid_lost"] += 1
            self._reacquire()
            return
        if n_lines == 0:
            return
        self._no_v_since_h += n_lines * 2.0
        if self._no_v_since_h > 2.2 * self.std.field_halves:
            self.stats.v_locked = False
            if self._last_vsync_h is None and self._pol != 0 and self.counters["pol_flips"] < 2:
                self._flip_polarity()              # picture features looked like sync: try the other way
                return
        hits = self._b_hits[:n_lines]
        hit_sum = int(np.count_nonzero(hits))
        if self._sync_failing(n_lines, hit_sum):
            self._reacquire()
            return
        self._hits_ewma += (hit_sum / n_lines - self._hits_ewma) * min(1.0, n_lines / 16.0)
        self._hits_ewma = float(self._hits_ewma)
        h_locked = self._hits_ewma > 0.5
        self.stats.h_locked = h_locked
        self.stats.lines_total += n_lines
        if h_locked:
            self.stats.lines_lost += n_lines - hit_sum
        # after the first missed lines keep looking for a pulse train that may sit elsewhere:
        # after a jump in the sample stream (or a signal change) the old grid is just wrong
        if st[k.ST_MISS] >= 4:
            self._rephase_search()
            if st[k.ST_MODE] == 0:
                return
        self._vertical(n_lines)

    def _sync_failing(self, n_lines: int, hit_sum: int) -> bool:
        """Most lines without a sync edge for a whole field. The flywheel covers shorter losses;
        beyond this the tracked levels can't be trusted (a fade corrupts them, and false pulses
        then keep them corrupted), so only a fresh acquisition can recover."""
        self._hit_lines += n_lines
        self._hit_count += hit_sum
        half_field = self.std.field_halves // 4
        if self._hit_lines < half_field:
            return False
        bad = self._hit_count < 0.5 * self._hit_lines
        self._hit_lines = self._hit_count = 0
        self._bad_windows = self._bad_windows + 1 if bad else 0
        return self._bad_windows >= 2

    def _reacquire(self) -> None:
        self.st[k.ST_MODE] = 0
        self._scan = {+1: 1, -1: 1}
        for q in self._pulses.values():
            q.clear()
        self._abandon_field()
        self._last_vsync_h = None
        self._hit_lines = self._hit_count = self._bad_windows = 0
        self.stats.h_locked = False
        self.stats.v_locked = False
        self.counters["reacquires"] += 1

    def _flip_polarity(self) -> None:
        self._pol = -self._pol
        self._v = -self._v
        self._y = -self._y
        self._box_hist = -self._box_hist
        self.stats.polarity = self._pol
        self.st[k.ST_MODE] = 0
        for q in self._pulses.values():
            q.clear()
        self._scan = {+1: self._n0 + 1, -1: self._n0 + 1}
        self._no_v_since_h = 0.0
        self.counters["pol_flips"] += 1

    def _free_run(self) -> None:
        """No sync: keep drawing lines on a free-running grid so the raw (noisy) picture shows."""
        s = self.std
        fs = self.fs
        P = fs / s.line_freq
        n0 = self._n0
        end = n0 + len(self._v) - int(P * 1.2)
        if self._free_next is None or self._free_next < n0 + 16:
            self._free_next = float(n0 + 16)
            self._free_row = 0
        cfg = self._cfg()
        cfg[8] = 0.0              # no colour on a free-running picture
        n = int(max((end - self._free_next) // P, 0))
        if n <= 0:
            return
        ts = self._free_next + np.arange(n) * P
        self._free_next += n * P
        lines = s.lines / 2.0
        h_lines = s.active_lines
        if self._free_field is None or self._free_field["y"].shape[0] != h_lines:
            shape = (h_lines, PIXELS)
            self._free_field = dict(y=np.zeros(shape, np.float32), u=np.zeros(shape, np.float32),
                                    v=np.zeros(shape, np.float32), filled=np.zeros(h_lines, np.uint8))
        f = self._free_field
        v = np.ascontiguousarray(self._v)
        m = float(np.mean(v))
        scale = abs(s.sync_v) / max(0.3 * 8e6, 1.0)
        for i in range(n):
            r = self._free_row - 20
            self._free_row += 1
            if 0 <= r < h_lines:
                k.decode_lines(v, n0, ts[i:i + 1], np.array([self._free_row]), np.array([r], np.int64), 1,
                               self.cs, cfg, self._cos, self._sin, f["y"], f["u"], f["v"], f["filled"],
                               np.array([m]), np.array([scale]), self._prev_uv)
            if self._free_row >= lines:
                self._free_row = 0
                planes = self._merge(f, f, doubled=True)
                self._emit_frame(planes, {"end": self._stream}, locked=False)
                self.stats.fields += 1

    def _rephase_search(self) -> None:
        """While flywheeling: is there a train at some other phase? (Sample stream jumped.)"""
        n0 = self._n0
        y = self._y
        N = len(y)
        fs = self.fs
        out = np.zeros(k.MAX_PULSES * 2)
        st = self.st
        tip, blank = st[k.ST_TIP], st[k.ST_BLANK]
        th = tip + 0.5 * (blank - tip)
        start = max(self._scan[+1] - n0, 1)
        if start >= N - int(10e-6 * fs):
            return
        cnt, nxt = k.scan_pulses(y, n0, start, N - int(10e-6 * fs), th, int(3.4e-6 * fs), int(6.8e-6 * fs), out)
        self._scan[+1] = n0 + int(nxt)
        q = self._pulses[+1]
        for i in range(cnt):
            q.append(float(out[i]))
        lo, hi = self._period_limits()
        lk = self._find_train(q, lo, hi)
        if lk is None:
            return
        period, last = lk
        phase = (last - (st[k.ST_NEXT])) % period
        phase = min(phase, period - phase)
        if phase > 2.5e-6 * fs:
            # the grid moved: continue from the new position
            st[k.ST_NEXT] = last + period
            st[k.ST_PERIOD] = period
            st[k.ST_MISS] = 0
            self._hits_ewma = 1.0
            self._set_standard(self._period_std(period))
            self.counters["rephases"] += 1
            if self._cur is not None:
                self._cur["dirty"] = True
            self.stats.relocks += 1
        for key in self._pulses:
            self._pulses[key].clear()

    def _vertical(self, n_lines: int) -> None:
        first_line = self._lines
        self._lines += n_lines
        st = self.st
        min_gap = int(self.std.field_halves * 0.7)
        ev = k.find_vsync(self._b_flags, n_lines, st, min_gap, self._ev_half)
        cursor = 0
        for e in range(ev):
            h = int(self._ev_half[e])          # half lines relative to first_line, may be < 0
            line = first_line + (h // 2)
            i = max(line - first_line, 0)
            if i > cursor:
                self._assemble(cursor, i, first_line)
                cursor = i
            t = float(self._b_lines[0]) + h * float(self.st[k.ST_PERIOD]) / 2.0
            self._on_vsync(line, h % 2, t)
        if cursor < n_lines:
            self._assemble(cursor, n_lines, first_line)
        self._vcount = (first_line + n_lines) * 2
        self._flywheel_vertical()

    def _flywheel_vertical(self) -> None:
        """A vertical sync that is overdue (lost in noise or a dropout) is replaced by the one
        expected from the previous field, so fields keep coming out on time."""
        if self._last_vsync_h is None or self._cur is None:
            return
        expect = self._last_vsync_h + self.std.field_halves
        if self._vcount < expect + 6:          # still within 3 lines of where it should have been
            return
        self._v_miss += 1
        self.counters["v_flywheel"] += 1
        if self._v_miss >= 5:
            self.stats.v_locked = False
        self._last_vsync_h = expect
        self._finish_field()
        self._begin_field(expect // 2, expect % 2, float(self._n0))

    def _on_vsync(self, line: int, half: int, t: float) -> None:
        h = line * 2 + half
        ok = True
        if self._last_vsync_h is not None:
            delta = h - self._last_vsync_h
            self._delta = delta
            if abs(delta - 625) <= 1:
                self._vote_standard("PAL")
            elif abs(delta - 525) <= 1:
                self._vote_standard("NTSC")
            else:
                ok = False
        self._last_vsync_h = h
        self._no_v_since_h = 0.0
        self.stats.v_locked = True
        self._v_miss = 0
        self._finish_field()
        self._begin_field(line, half, t)
        return ok

    def _vote_standard(self, name: str) -> None:
        self._set_standard(name)

    # -------------------------------------------------------------------- field assembly
    def _abandon_field(self) -> None:
        if self._cur is not None:
            self.stats.dropped_fields += 1
        self._cur = None
        self._weave = None

    def _begin_field(self, line: int, half: int, t: float) -> None:
        s = self.std
        cur = self._spare_field
        if cur is not None and cur["y"].shape[0] == s.active_lines:
            self._spare_field = None
        else:
            shape = (s.active_lines, PIXELS)
            cur = dict(y=np.zeros(shape, np.float32), u=np.zeros(shape, np.float32), v=np.zeros(shape, np.float32),
                       filled=np.zeros(s.active_lines, np.uint8))
        cur["parity"] = half
        cur["filled"][:] = 0
        cur["anchor"] = line
        cur["start"] = t
        cur["lost"] = 0
        cur["dirty"] = False
        cur["first"] = s.first_active[half]
        self._cur = cur
        self._update_colour_lock()

    def _finish_field(self) -> None:
        f = self._cur
        if f is None:
            return
        self._cur = None
        n_rows = int(np.count_nonzero(f["filled"]))
        s = self.std
        complete = n_rows >= int(s.active_lines * 0.97)
        clean = complete and not f["dirty"] and f["y"].shape[0] == s.active_lines and self.stats.h_locked
        self.stats.fields += 1
        self.field_log.append(FieldInfo(int(f["start"]), int(f["parity"]), clean, s.name, int(f["lost"]), n_rows))
        if not clean:
            if self.stats.h_locked:
                self.stats.dropped_fields += 1
            self._weave = None
            self._spare_field = f
            return
        mode = self.params.deinterlace
        if mode == "double":
            self._spare_field = None
            self._emit_frame(self._merge(f, f, doubled=True), f)
            return
        partner = self._weave
        if f["parity"] == 0:
            self._weave = f
        else:
            self._weave = None
            if partner is not None and partner["parity"] == 0:
                self._emit_frame(self._merge(partner, f), f)
                self._spare_field = None
            else:
                self._spare_field = f
                self.stats.dropped_fields += 1

    def _merge(self, f1: dict, f2: dict, doubled: bool = False):
        h = f1["y"].shape[0]
        y = np.empty((h * 2, PIXELS), np.float32)
        u = np.empty((h * 2, PIXELS), np.float32)
        v = np.empty((h * 2, PIXELS), np.float32)
        for name, plane in (("y", y), ("u", u), ("v", v)):
            if doubled:
                plane[0::2] = f1[name]
                plane[1::2] = f1[name]
            else:
                plane[0::2] = f1[name]
                plane[1::2] = f2[name]
        return y, u, v

    def _emit_frame(self, planes, last_field: dict, locked: bool = True) -> None:
        y, u, v = planes
        rgb = self._render(y, u, v)
        self._frame_no += 1
        arrival = self._arrival_for(int(last_field.get("end", self._stream)))
        fr = VideoFrame(rgb, y, u, v, self.std.name, self._frame_no, locked, self._colour, self._stream, arrival)
        self.frames.append(fr)
        self.stats.frames += 1
        if self.on_frame is not None:
            self.on_frame(fr)

    def _arrival_for(self, pos: int) -> float:
        q = self._pending_arrivals
        best = 0.0
        while q and q[0][0] <= self._stream:
            best = q.popleft()[1]
        return best

    def _render(self, y, u, v) -> np.ndarray:
        p = self.params
        rgb = np.empty(y.shape + (3,), np.uint8)
        k.render_rgb(y, u, v, float(p.contrast), float(p.brightness), float(p.saturation),
                     bool(self._colour and p.colour), rgb)
        return rgb

    def _update_colour_lock(self) -> None:
        cs = self.cs
        lock = float(cs[k.CS_LOCK])
        mag = float(cs[k.CS_ACC])
        s = self.std
        h = max(self.st[k.ST_BLANK] - self.st[k.ST_TIP], 1e-3)
        want = abs(s.burst_v / s.sync_v) * h
        # coherent burst of a plausible size, with a little hysteresis over fields
        good = lock > 0.55 and mag > 0.25 * want
        if good:
            self._colour_hold = min(self._colour_hold + 1, 4)
        else:
            self._colour_hold = max(self._colour_hold - 1, 0)
        was = self._colour
        self._colour = self.params.colour and self._colour_hold >= 2
        if not self.params.colour:
            self._colour_hold = 0
        self.stats.colour_locked = self._colour
        if self._colour != was:
            self._prev_uv[:] = 0.0

    def _assemble(self, a: int, b: int, first_line: int) -> None:
        if self._cur is None:
            return
        cur = self._cur
        s = self.std
        h_lines = cur["y"].shape[0]
        lines_nos = first_line + np.arange(a, b)
        first_row = cur["anchor"] + cur["first"] + self._v_shift_rows()
        rows = lines_nos - first_row
        idx = np.nonzero((rows >= 0) & (rows < h_lines))[0]
        if len(idx) == 0:
            return
        sel = a + idx
        cfg = self._cfg()
        sync_h = max(self.st[k.ST_BLANK] - self.st[k.ST_TIP], 1.0)
        p = self.params
        if p.agc:
            scale = abs(s.sync_v) / sync_h
        else:
            scale = (10 ** (p.gain_db / 20.0)) / max(self.fs * 0.4, 1.0)
        n = len(sel)
        scales = np.full(n, scale)
        blanks = self._blank_trace(sel)
        k.decode_lines(np.ascontiguousarray(self._v), self._n0, self._line_times(sel), lines_nos[idx],
                       rows[idx].astype(np.int64), n, self.cs, cfg, self._cos, self._sin, cur["y"], cur["u"], cur["v"],
                       cur["filled"], blanks, scales, self._prev_uv)
        # stats per field
        hits = self._b_hits[sel]
        cur["lost"] += int(n - np.count_nonzero(hits))
        cur["end"] = self._stream

    def _v_shift_rows(self) -> int:
        return int(self.params.v_shift) // 2

    def _blank_trace(self, sel: np.ndarray) -> np.ndarray:
        """Black level per line: back-porch measurements smoothed along the field."""
        raw = self._b_blanks[sel]
        out = np.empty(len(raw))
        a = 0.2
        last = self._last_blank
        for i, x in enumerate(raw):
            last = last + a * (x - last)
            out[i] = last
        self._last_blank = last
        return out

    _last_blank = 0.0

    def _line_times(self, sel: np.ndarray) -> np.ndarray:
        """Line origins as times in the v buffer: the sync filter lags v by (box-1)/2 samples."""
        return self._b_lines[sel] - (self._box - 1) / 2.0

    def _cfg(self) -> np.ndarray:
        s = self.std
        fs = self.fs
        p = self.params
        self._ensure_tables(s)
        px_step = PIXEL_US * 1e-6 * fs
        px0 = (s.grid_start_us + p.h_shift_us) * 1e-6 * fs
        burst_a = int(round(s.burst_start_us * 1e-6 * fs + 0.15e-6 * fs))
        burst_b = int(round((s.burst_start_us * 1e-6 + (s.burst_cycles - 0.7) / s.fsc) * fs))
        span = int(math.ceil((s.grid_start_us + PIXELS * PIXEL_US + 1.0) * 1e-6 * fs)) + 8
        colour_on = 1.0 if (self.params.colour and self._colour) else 0.0
        target = (s.burst_v / s.chroma_v) * (0.7071 if s.pal else 1.0)
        return np.array([fs, s.fsc, px0, px_step, PIXELS, burst_a, burst_b, 1.0 if s.pal else 0.0, colour_on,
                         s.setup_v, 1.0 / (s.white_v - s.setup_v), target, span, fs / s.line_freq, 1.0, self._L1, self._L2])

    def _ensure_tables(self, s) -> None:
        if getattr(self, "_tab_std", None) is s:
            return
        self._tab_std = s
        # two cascaded moving averages for the chroma low-pass, about two cycles long: the
        # first null falls near 2*fsc, the mixing product
        fs = self.fs
        self._L1 = max(int(round(2 * fs / s.fsc)) | 1, 3)
        self._L2 = max(int(round(1.6 * fs / s.fsc)) | 1, 3)
        n = int(fs / s.line_freq) + 64
        w = 2 * np.pi * s.fsc / fs * np.arange(n, dtype=np.float64)
        self._cos = np.cos(w).astype(np.float32)
        self._sin = np.sin(w).astype(np.float32)

    @property
    def standard_name(self) -> str:
        return self.std.name

    def take_frames(self) -> list[VideoFrame]:
        out = list(self.frames)
        self.frames.clear()
        return out

    @staticmethod
    def expected_lines(std: VideoStandard) -> int:
        return std.active_lines
