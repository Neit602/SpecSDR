"""FPV video engine parts (no Qt): the decode worker thread and the frequency scanner.

The engine's DSP thread hands IQ blocks to the worker through a bounded queue. If decoding falls
behind, whole blocks are dropped and the decoder is told, so it drops whole fields and resyncs
instead of building a backlog."""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from ..dsp.fpv.audio import FpvAudio
from ..dsp.fpv.decoder import FpvDecoder, FpvParams
from ..dsp.fpv.detect import detect_video

log = logging.getLogger(__name__)


class FpvWorker:
    """Decoder thread. push() never blocks; on_frame runs on this thread (the GUI side
    forwards it through a Qt signal)."""

    MAX_BACKLOG_S = 0.12            # at most this much unprocessed IQ before shedding load

    def __init__(self, fs: float, params: FpvParams, on_frame=None, sink=None):
        self.fs = float(fs)
        self.params = params
        self.on_frame = on_frame
        self.sink = sink
        self.audio = FpvAudio(fs, sink.sample_rate if sink else 48000.0)
        self.decoder = FpvDecoder(fs, params, audio=self.audio)
        self.decoder.on_frame = self._frame
        self._q: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self._backlog = 0
        self._stop = False
        self._paused = False
        self.max_backlog = int(self.fs * self.MAX_BACKLOG_S)
        self.dropped_blocks = 0
        self.dropped_samples = 0
        self._fps_t = time.monotonic()
        self._fps_n = 0
        self.fps = 0.0
        self.latency_ms = 0.0
        self.last_frame = None
        self.drift_cb = None         # called with the number of new audio frames (engine drift loop)
        self._cmds: collections.deque = collections.deque()
        self._warming = True
        self._thread = threading.Thread(target=self._run, name="fpv-decode", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- called from other threads
    def push(self, iq: np.ndarray, arrival: float | None = None) -> None:
        if self._stop or self._paused or self._warming:
            return
        with self._cv:
            if self._backlog + len(iq) > self.max_backlog:
                dropped = self._backlog + len(iq)
                self.dropped_blocks += len(self._q) + 1
                self.dropped_samples += dropped
                self._q.clear()
                self._backlog = 0
                self._q.append((None, dropped))     # gap marker: the decoder resyncs at the next field
                self._cv.notify()
                return
            self._q.append((iq, arrival))
            self._backlog += len(iq)
            self._cv.notify()

    def post(self, fn) -> None:
        """Run fn on the decoder thread (parameter changes)."""
        with self._cv:
            self._cmds.append(fn)
            self._cv.notify()

    def set_paused(self, on: bool) -> None:
        """Pause during a scan: while the centre frequency is jumping there is nothing to decode."""
        self._paused = on
        if on:
            with self._cv:
                self._q.clear()
                self._backlog = 0
        else:
            self.post(lambda: self.decoder.notify_gap(int(self.fs)))

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(timeout=3.0)

    def status(self):
        return self.decoder.stats

    def _frame(self, frame) -> None:
        now = time.monotonic()
        self._fps_n += 1
        if now - self._fps_t >= 1.0:
            self.fps = self._fps_n / (now - self._fps_t)
            self._fps_t = now
            self._fps_n = 0
        if frame.arrival:
            self.latency_ms = 0.8 * self.latency_ms + 0.2 * (now - frame.arrival) * 1000.0
        self.last_frame = frame
        self.decoder.stats.fps = self.fps
        self.decoder.stats.latency_ms = self.latency_ms
        if self.on_frame is not None:
            self.on_frame(frame)

    # ------------------------------------------------------------- decoder thread
    def _run(self) -> None:
        dec = self.decoder
        try:
            FpvDecoder.warmup(self.fs)
        except Exception:
            log.exception("FPV decoder warm-up")
        self._warming = False
        while True:
            with self._cv:
                while not self._q and not self._cmds and not self._stop:
                    self._cv.wait(0.2)
                if self._stop:
                    break
                cmds = list(self._cmds)
                self._cmds.clear()
                items = list(self._q)
                self._q.clear()
                self._backlog -= sum(len(i[0]) for i in items if i[0] is not None)
            for fn in cmds:
                fn()
            for iq, extra in items:
                try:
                    if iq is None:
                        dec.notify_gap(int(extra))
                    else:
                        dec.process(iq, extra)
                        self._pump_audio()
                except Exception:
                    log.exception("FPV decoder error")
        dec.flush()

    def _pump_audio(self) -> None:
        if self.sink is None or not self.audio.enabled:
            return
        a = self.audio.take()
        if len(a):
            self.sink.write(a)
            if self.drift_cb is not None:
                self.drift_cb(len(a))


# --------------------------------------------------------------------------- scanner

@dataclass
class ScanResult:
    frequency: float
    channel: str
    standard: str
    level_db: float
    last_seen: float
    quality: float


class FpvScanner:
    """Steps the source through a list of frequencies, detecting analog video at each. Captures
    come from the engine's block tap (the normal stream keeps running)."""

    CAPTURE_S = 0.025
    MERGE_HZ = 7e6              # detections this close are the same transmitter

    def __init__(self, engine, namer=None):
        self.engine = engine
        self.namer = namer or (lambda f: "")
        self.results: dict[int, ScanResult] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self.on_update = None
        self.on_finished = None
        self.step_done = 0
        self.steps_total = 0
        self.last_duration = 0.0
        self.continuous = False
        self.error = ""

    @property
    def running(self) -> bool:
        return self._running

    def start(self, frequencies, continuous: bool = False) -> None:
        if self._running:
            return
        self.continuous = bool(continuous)
        self._freqs = list(frequencies)
        self._stop.clear()
        self._running = True
        self.engine.fpv_pause(True)
        self._thread = threading.Thread(target=self._run, name="fpv-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None

    def _capture(self, target_f: float, settle_s: float) -> np.ndarray | None:
        """Wait for samples from the new frequency, skip the settling time, grab the capture."""
        fs = self.engine.source.sample_rate
        need = int(self.CAPTURE_S * fs)
        settle = int(settle_s * fs)
        buf: list[np.ndarray] = []
        got = [0, settle]            # have, still to skip
        done = threading.Event()

        def tap(x, fc, _fs):
            if fc != target_f or got[0] >= need:
                return
            k = len(x)
            skip = min(got[1], k)
            got[1] -= skip
            if skip < k:
                part = x[skip:]
                buf.append(part)
                got[0] += len(part)
            if got[0] >= need:
                done.set()

        self.engine.set_tap(tap)
        ok = done.wait(2.0 + settle_s + self.CAPTURE_S)
        self.engine.set_tap(None)
        if self._stop.is_set() or not ok:
            return None
        return np.concatenate(buf)[:need]

    def _run(self) -> None:
        source = self.engine.source
        settle = getattr(source, "retune_settle_s", 0.0)
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                self.steps_total = len(self._freqs)
                for i, f in enumerate(self._freqs):
                    if self._stop.is_set():
                        break
                    source.set_center_frequency(f)
                    x = self._capture(source.center_frequency, settle)
                    self.step_done = i + 1
                    if x is None:
                        continue
                    det = detect_video(x, source.sample_rate)
                    if det is not None:
                        self._add(f + det.frequency_offset, det)
                    if self.on_update is not None:
                        self.on_update()
                self.last_duration = time.monotonic() - t0
                if not self.continuous:
                    break
        except Exception as e:  # reported to the user via the panel
            log.exception("scanner")
            self.error = str(e)
        finally:
            self._running = False
            self.engine.fpv_pause(False)
            if self.on_finished is not None:
                self.on_finished()

    def _add(self, freq: float, det) -> None:
        key = round(freq / self.MERGE_HZ)
        for k2 in (key - 1, key, key + 1):
            r = self.results.get(k2)
            if r is not None and abs(r.frequency - freq) <= self.MERGE_HZ:
                r.last_seen = time.time()
                if det.level_db > r.level_db:
                    r.frequency, r.level_db, r.standard = freq, det.level_db, det.standard
                    r.channel = self.namer(freq)
                return
        self.results[key] = ScanResult(freq, self.namer(freq), det.standard, det.level_db, time.time(), det.quality)

    def strongest(self) -> ScanResult | None:
        return max(self.results.values(), key=lambda r: r.level_db, default=None)
