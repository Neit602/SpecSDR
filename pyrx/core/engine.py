"""Streaming engine: source thread -> bounded queue -> DSP thread -> audio sink, spectrum frames.

No Qt here. The GUI talks to the engine with thread-safe setters (they post commands that
the DSP thread applies between blocks) and polls `take_frames()`, `take_events()` and
`stats()` from a timer (that is what caps the GUI update rate)."""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field

import numpy as np

from ..audio.sink import AudioSink, NullSink
from ..dsp.dc import DcRemover
from ..dsp.receiver import AgcSettings, ReceiverChain
from ..dsp.spectrum import SpectrumAnalyzer, SpectrumFrame, noise_in_band_db
from ..sources.base import Source, SourceError
from .recorder import Recorder, audio_recorder, iq_recorder

log = logging.getLogger(__name__)


@dataclass
class RadioParams:
    mode: str = "WFM"
    bandwidth: float | None = None
    filter_window: str = "blackmanharris"
    filter_order: int | None = None
    deemphasis_tau: float = 50e-6
    stereo: bool = True
    cw_pitch: float = 600.0
    squelch_enabled: bool = False
    squelch_db: float = -60.0
    agc: AgcSettings = field(default_factory=AgcSettings)


@dataclass
class EngineStats:
    running: bool = False
    sample_rate: float = 0.0          # nominal
    effective_rate: float = 0.0       # measured samples/s arriving from the source
    blocks: int = 0
    dropped_blocks: int = 0           # engine queue full
    source_overflows: int = 0         # reported by the source/driver
    dsp_errors: int = 0
    level_db: float = -150.0          # channel power at the VFO, dBFS
    snr_db: float = 0.0
    squelch_open: bool = True
    stereo: bool = False
    audio_underruns: int = 0          # jitter buffer ran dry + device underflows
    device_underflows: int = 0
    audio_overruns: int = 0
    audio_fill_ms: float = 0.0
    drift_ppm: float = 0.0
    dsp_load: float = 0.0             # fraction of real time spent in the DSP thread
    channel_rate: float = 0.0
    agc_gain_db: float = 0.0


class Engine:
    QUEUE_BLOCKS = 48
    MAX_FRAMES = 240
    WATCHDOG_S = 3.0

    def __init__(self, sink: AudioSink | None = None):
        self.source: Source | None = None
        self.sink: AudioSink = sink or NullSink()
        self.params = RadioParams()
        self.vfo = 100e6
        self.dc_removal = True
        self.swap_iq = False
        self._fft = dict(fft_size=8192, window="blackmanharris", frame_rate=30.0, averaging=1, waterfall_rate=30.0)
        self.spectrum = SpectrumAnalyzer(**self._fft)
        self.chain: ReceiverChain | None = None
        self._fs = 0.0
        self._dc: DcRemover | None = None
        self._q: queue.Queue = queue.Queue(maxsize=self.QUEUE_BLOCKS)
        self._cmds: collections.deque = collections.deque()
        self._frames: list[SpectrumFrame] = []
        self._frames_lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=50)
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._running = False
        self._state_lock = threading.RLock()
        self._last_block_t = 0.0
        self._rate_window: collections.deque = collections.deque()
        self._last_frame: SpectrumFrame | None = None
        self._drift = 1.0
        self._fill_avg: float | None = None
        self._fill_ref = 0.0
        self._busy = 0.0
        self._busy_t0 = time.monotonic()
        self._error_reported = False
        self._seen_errors: set[str] = set()
        self.st = EngineStats()
        self.iq_rec: Recorder | None = None
        self.af_rec: Recorder | None = None

    # ================================================================ lifecycle
    @property
    def running(self) -> bool:
        return self._running

    def set_source(self, source: Source | None) -> None:
        if self._running:
            raise RuntimeError("stop streaming before changing the source")
        self.source = source

    def set_sink(self, sink: AudioSink) -> None:
        old = self.sink
        was = old.running
        if was:
            old.stop()
        self.sink = sink
        self._post(lambda: self._rebuild_chain())
        if was:
            sink.start()

    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            if self.source is None:
                raise SourceError("No source selected")
            if not self.source.is_open:
                self.source.open()
            self._drain_queue()
            self._stop_evt.clear()
            self._error_reported = False
            self._last_block_t = time.monotonic()
            self._rate_window.clear()
            self._fill_avg = None
            self._fs = 0.0
            self.sink.jitter.clear()
            self._thread = threading.Thread(target=self._dsp_loop, name="dsp", daemon=True)
            self._running = True
            self._thread.start()
            try:
                self.sink.start()
            except Exception as e:
                log.error("audio start failed: %s", e)
                self._event("warning", f"Audio output failed to start ({e}); continuing without sound.")
                self.sink = NullSink(self.sink.sample_rate)
                self.sink.start()
            try:
                self.source.start(self._on_block, self._on_source_error)
            except Exception:
                self._stop_dsp()
                self.sink.stop()
                self._running = False
                raise

    def stop(self) -> None:
        with self._state_lock:
            if not self._running and self._thread is None:
                return
            src = self.source
            # audio first: otherwise the sound card drains the buffer while the source and
            # DSP shut down and that shows up as a (harmless, but misleading) underrun
            self.sink.stop()
            if src is not None:
                self._call_with_timeout(src.stop, 3.0, "stopping the source")
            self._stop_dsp()
            self._running = False
            for rec in (self.iq_rec, self.af_rec):
                if rec and rec.active:
                    rec.stop()

    def shutdown(self) -> None:
        self.stop()
        if self.source is not None:
            self._call_with_timeout(self.source.close, 3.0, "closing the device")

    @staticmethod
    def _call_with_timeout(fn, timeout: float, what: str) -> bool:
        """Run fn on a helper thread; never let a wedged driver hang the caller."""
        done = threading.Event()
        err: list = []

        def run():
            try:
                fn()
            except Exception as e:  # reported, not raised
                err.append(e)
            finally:
                done.set()

        t = threading.Thread(target=run, name="stopper", daemon=True)
        t.start()
        if not done.wait(timeout):
            log.error("timed out %s; abandoning it", what)
            return False
        if err:
            log.warning("error %s: %s", what, err[0])
        return True

    def _stop_dsp(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._thread = None

    def _drain_queue(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    # ================================================================ source callbacks
    def _on_block(self, samples: np.ndarray, center: float, rate: float) -> None:
        now = time.monotonic()
        self._last_block_t = now
        self._rate_window.append((now, len(samples)))
        try:
            self._q.put_nowait((samples, center, rate))
        except queue.Full:
            self.st.dropped_blocks += 1

    def _on_source_error(self, message: str, kind: str = "error") -> None:
        if self._error_reported:
            return
        self._error_reported = True
        self._event(kind, message)
        # stop from another thread: the source's own thread must be free to exit
        threading.Thread(target=self.stop, name="error-stop", daemon=True).start()

    def _event(self, kind: str, message: str) -> None:
        self._events.append((kind, message))

    # ================================================================ GUI-side API
    def take_frames(self) -> list[SpectrumFrame]:
        """Frames whose release time has come (see _process)."""
        now = time.monotonic()
        with self._frames_lock:
            n = 0
            for t, _ in self._frames:
                if t > now:
                    break
                n += 1
            out = [f for _, f in self._frames[:n]]
            del self._frames[:n]
        return out

    def take_events(self) -> list[tuple[str, str]]:
        out = []
        while self._events:
            out.append(self._events.popleft())
        return out

    def stats(self) -> EngineStats:
        st = self.st
        st.running = self._running
        src = self.source
        st.sample_rate = src.sample_rate if src else 0.0
        st.source_overflows = src.overflows if src else 0
        now = time.monotonic()
        w = self._rate_window
        while w and now - w[0][0] > 2.0:
            w.popleft()
        if len(w) >= 2 and self._running:
            items = list(w)
            span = max(items[-1][0] - items[0][0], 1e-3)
            st.effective_rate = sum(n for _, n in items[1:]) / span
        else:
            st.effective_rate = 0.0
        st.audio_underruns = self.sink.underruns
        st.device_underflows = self.sink.device_underflows
        st.audio_overruns = self.sink.overruns
        st.audio_fill_ms = self.sink.jitter.fill_seconds * 1000
        st.drift_ppm = (self._drift - 1.0) * 1e6
        dt = now - self._busy_t0
        if dt >= 0.5:
            st.dsp_load = self._busy / dt
            self._busy = 0.0
            self._busy_t0 = now
        return st

    def _post(self, fn) -> None:
        """Run fn on the DSP thread before the next block (or now, if not streaming)."""
        if self._running and self._thread is not None:
            self._cmds.append(fn)
        else:
            fn()

    def set_vfo(self, hz: float) -> None:
        self.vfo = float(hz)

    def set_mode(self, mode: str, bandwidth: float | None = None, order: int | None = None) -> None:
        p = self.params
        p.mode, p.bandwidth, p.filter_order = mode, bandwidth, order
        self._post(lambda: self.chain and self.chain.set_mode(mode, bandwidth, order))

    def set_bandwidth(self, bw: float) -> None:
        self.params.bandwidth = bw
        self._post(lambda: self.chain and self.chain.set_bandwidth(bw))

    def set_filter(self, window: str | None = None, order: int | None = None) -> None:
        if window:
            self.params.filter_window = window
        if order:
            self.params.filter_order = order
        self._post(lambda: self.chain and self.chain.set_filter(window, order))

    def set_deemphasis(self, tau: float) -> None:
        self.params.deemphasis_tau = tau
        self._post(lambda: self.chain and self.chain.set_deemphasis(tau))

    def set_stereo(self, on: bool) -> None:
        self.params.stereo = on
        self._post(lambda: self.chain and self.chain.set_stereo(on))

    def set_cw_pitch(self, hz: float) -> None:
        self.params.cw_pitch = hz
        self._post(lambda: self.chain and self.chain.set_cw_pitch(hz))

    def set_squelch(self, enabled: bool | None = None, threshold_db: float | None = None) -> None:
        if enabled is not None:
            self.params.squelch_enabled = enabled
        if threshold_db is not None:
            self.params.squelch_db = threshold_db
        self._post(lambda: self.chain and self.chain.set_squelch(enabled, threshold_db))

    def set_agc(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self.params.agc, k, v)
        self._post(lambda: self.chain and self.chain.set_agc(**kw))

    def set_fft(self, **kw) -> None:
        self._fft.update({k: v for k, v in kw.items() if v is not None})
        cfg = dict(self._fft)

        def apply():
            self.spectrum.configure(**cfg)
            if "fft_size" in kw or "averaging" in kw:
                self.spectrum.reset()
        self._post(apply)

    def set_dc_removal(self, on: bool) -> None:
        self.dc_removal = bool(on)

    def set_swap_iq(self, on: bool) -> None:
        self.swap_iq = bool(on)

    def last_frame(self) -> SpectrumFrame | None:
        return self._last_frame

    # ---------------------------------------------------------------- recording
    def start_iq_recording(self, directory: str, fmt: str = "cs16") -> Recorder:
        src = self.source
        if src is None or not self._running:
            raise RuntimeError("start streaming before recording")
        rec = iq_recorder(directory, src.center_frequency, src.sample_rate, fmt)
        rec.start()
        self.iq_rec = rec
        return rec

    def stop_iq_recording(self) -> None:
        if self.iq_rec:
            self.iq_rec.stop()

    def start_audio_recording(self, directory: str) -> Recorder:
        if not self._running:
            raise RuntimeError("start streaming before recording")
        rec = audio_recorder(directory, self.vfo, self.sink.sample_rate, 2)
        rec.start()
        self.af_rec = rec
        return rec

    def stop_audio_recording(self) -> None:
        if self.af_rec:
            self.af_rec.stop()

    # ================================================================ DSP thread
    def _rebuild_chain(self) -> None:
        if not self._fs:
            self.chain = None
            return
        p = self.params
        self.chain = ReceiverChain(self._fs, self.sink.sample_rate, p.mode, p.bandwidth, 0.0,
                                   p.filter_window, p.filter_order, p.deemphasis_tau, p.stereo,
                                   p.cw_pitch, p.squelch_enabled, p.squelch_db, p.agc)
        p.bandwidth = self.chain.bandwidth
        p.filter_order = self.chain.filter_order
        self.chain.set_drift(self._drift)

    def _reconfigure(self, fs: float) -> None:
        self._fs = fs
        self._dc = DcRemover(fs)
        self.spectrum.reset()
        self._rebuild_chain()

    def _dsp_loop(self) -> None:
        while not self._stop_evt.is_set():
            while self._cmds:
                fn = self._cmds.popleft()
                try:
                    fn()
                except Exception:
                    self._dsp_exception()
            try:
                x, fc, fs = self._q.get(timeout=0.1)
            except queue.Empty:
                if (self._running and not self._error_reported
                        and time.monotonic() - self._last_block_t > self.WATCHDOG_S):
                    self._on_source_error(f"No samples received from the device for {self.WATCHDOG_S:.0f} s. "
                                          "It may have been unplugged.")
                continue
            t0 = time.perf_counter()
            try:
                self._process(x, fc, fs)
            except Exception:
                self._dsp_exception()
            self._busy += time.perf_counter() - t0

    def _dsp_exception(self) -> None:
        self.st.dsp_errors += 1
        tb = traceback.format_exc()
        key = tb.strip().splitlines()[-1]
        if key not in self._seen_errors:  # log each distinct error once, never spam
            self._seen_errors.add(key)
            log.error("DSP error (further identical errors suppressed):\n%s", tb)
            self._event("error", f"Internal DSP error: {key}")

    def _process(self, x: np.ndarray, fc: float, fs: float) -> None:
        if fs != self._fs or self.chain is None:
            self._reconfigure(fs)
        rec = self.iq_rec
        if rec is not None and rec.active:
            rec.push(x)
        if self.swap_iq:
            x = (1j * np.conj(x)).astype(np.complex64)
        if self.dc_removal:
            x = self._dc.process(x)
        frames = self.spectrum.process(x, fc, fs)
        if frames:
            self._last_frame = frames[-1]
            # A block arrives all at once, but its frames describe times spread over the
            # block. Release them spread over the next block duration, so the display gets an
            # evenly paced stream even with long USB blocks (adds <= one block of latency).
            now = time.monotonic()
            dur = min(len(x) / fs, 0.25)
            with self._frames_lock:
                self._frames.extend((now + f.pos * dur, f) for f in frames)
                if len(self._frames) > self.MAX_FRAMES:
                    del self._frames[:len(self._frames) - self.MAX_FRAMES]
        chain = self.chain
        chain.set_offset(self.vfo - fc)
        audio = chain.process(x)
        self._update_drift(len(audio))
        self.sink.write(audio)
        arec = self.af_rec
        if arec is not None and arec.active:
            arec.push(audio)
        st = self.st
        st.blocks += 1
        st.level_db = chain.channel_level_db
        st.squelch_open = chain.squelch_open
        st.stereo = chain.stereo_detected
        st.channel_rate = chain.fs_ch
        st.agc_gain_db = chain.agc.gain_db if chain.agc_settings.enabled else chain.agc_settings.manual_gain_db
        lf = self._last_frame
        if lf is not None:
            st.snr_db = st.level_db - noise_in_band_db(lf, chain.bandwidth)

    def _update_drift(self, n_new: int) -> None:
        """Keep the audio jitter buffer at its target by nudging the resampling ratio
        (SDR clock vs sound-card clock). Block-wise writing makes the fill a sawtooth, so
        the regulated quantity is its midpoint: fill just before the write + half the block.
        Proportional control (~30 s time constant) on that, smoothed, clamped to +-0.2 %.
        (A 10 s loop amplified the ~20 ms sound-server period jitter to +-1000 ppm swings.)"""
        jb = self.sink.jitter
        if jb._buffering:  # pre-filling after start/underrun: re-acquire the reference after
            self._fill_avg = None
            return
        fill = jb.fill + n_new / 2
        if self._fill_avg is None:
            # Hold the latency we start with (it depends on the source's block size) rather
            # than steering to the nominal target: that would bend the pitch for ~10 s after
            # every start. Only a too-small start latency is pulled up.
            self._fill_avg = float(fill)
            self._fill_ref = max(float(fill), 0.6 * jb.target)
        self._fill_avg += 0.02 * (fill - self._fill_avg)
        err_s = (self._fill_avg - self._fill_ref) / jb.sample_rate
        ratio = 1.0 - 0.03 * err_s
        ratio = min(max(ratio, 1 - 0.002), 1 + 0.002)
        if abs(ratio - self._drift) > 1e-7:
            self._drift = ratio
            if self.chain is not None:
                self.chain.set_drift(ratio)
