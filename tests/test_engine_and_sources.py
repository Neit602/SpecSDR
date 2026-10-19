"""Acceptance test 8 (file round trip) plus engine / source / audio behaviour."""

import glob
import os
import threading
import time

import numpy as np
import pytest

from pyrx.audio.sink import JitterBuffer, NullSink
from pyrx.core.engine import Engine
from pyrx.core.recorder import iq_recorder
from pyrx.dsp.spectrum import SpectrumAnalyzer
from pyrx.sources.base import Source, SourceError
from pyrx.sources.iqfile import IqFileSource
from pyrx.sources.simulated import SimulatedSource


def peak_frequency(blocks, center, rate, size=8192):
    sa = SpectrumAnalyzer(size)
    db = sa.compute(np.concatenate(blocks)[-size * 4:])
    k = int(np.argmax(db))
    return center + (k - size // 2) * rate / size


@pytest.mark.parametrize("fmt", ["cs16", "cu8", "cf32"])
def test_file_round_trip_same_peak(tmp_path, fmt):
    sim = SimulatedSource()
    sim.open()
    for s in sim.signals:  # one strong carrier only: CW beacon key-down is intermittent, use AM
        sim.set_control(f"{s.key}.enabled", s.key == "am")
    sim.set_control("noise_db", -70)
    rec = iq_recorder(str(tmp_path), sim.center_frequency, sim.sample_rate, fmt)
    rec.start()
    blocks = []
    for _ in range(40):
        x, fc, fs = sim.generate_block()
        blocks.append(x)
        rec.push(x)
    rec.stop()
    assert rec.error is None and rec.dropped == 0
    f_live = peak_frequency(blocks, sim.center_frequency, sim.sample_rate)
    assert abs(f_live - 100.6e6) < 2 * sim.sample_rate / 8192

    files = glob.glob(os.path.join(str(tmp_path), "*.wav"))
    assert len(files) == 1
    src = IqFileSource()
    src.open(files[0])  # centre + rate come from the file name
    assert src.center_frequency == sim.center_frequency
    assert src.sample_rate == sim.sample_rate
    src.set_control("loop", False)
    played = []
    while True:
        y, eof = src.read_block(48000)
        if len(y):
            played.append(y)
        if eof:
            break
    assert sum(map(len, played)) == sum(map(len, blocks))
    f_file = peak_frequency(played, src.center_frequency, src.sample_rate)
    assert f_file == f_live


def test_engine_streams_simulated_source():
    sim = SimulatedSource()
    eng = Engine(NullSink())
    eng.set_source(sim)
    eng.set_vfo(99.8e6)
    eng.set_mode("WFM")
    eng.start()
    try:
        time.sleep(2.5)
        frames = eng.take_frames()
        st = eng.stats()
    finally:
        eng.stop()
    assert st.running
    assert len(frames) > 20
    assert st.dropped_blocks == 0 and st.dsp_errors == 0
    assert st.level_db > -35  # the WFM station is at -25 dBFS
    assert st.stereo
    assert 2.2e6 < st.effective_rate < 2.6e6
    assert not eng.running


class FailingSource(Source):
    """Delivers a few blocks, then reports an error like an unplugged device."""
    name = "failing"
    display_name = "Failing"

    def open(self, device_id=None):
        self._open = True

    def close(self):
        self._open = False

    def start(self, cb, err=None):
        self._running = True

        def run():
            for _ in range(5):
                cb(np.zeros(24000, np.complex64), self._center, self._rate)
                time.sleep(0.01)
            self._running = False
            err("device unplugged", "error")
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self._running = False


class SilentSource(FailingSource):
    """Starts, then never delivers anything (wedged driver)."""

    def start(self, cb, err=None):
        self._running = True


def test_engine_stops_once_on_source_error():
    eng = Engine(NullSink())
    eng.set_source(FailingSource())
    eng.start()
    time.sleep(1.0)
    events = eng.take_events()
    assert events == [("error", "device unplugged")]
    assert not eng.running


def test_engine_watchdog_detects_dead_device():
    eng = Engine(NullSink())
    eng.WATCHDOG_S = 0.5
    eng.set_source(SilentSource())
    eng.start()
    time.sleep(1.5)
    ev = eng.take_events()
    assert len(ev) == 1 and ev[0][0] == "error" and "No samples" in ev[0][1]
    assert not eng.running


def test_engine_counts_dropped_blocks_instead_of_blocking():
    eng = Engine(NullSink())
    eng.QUEUE_BLOCKS = 2
    eng._q.maxsize = 2
    for _ in range(10):  # DSP thread not running: queue fills up
        eng._on_block(np.zeros(10, np.complex64), 0.0, 1e6)
    assert eng.st.dropped_blocks == 8


def test_jitter_buffer_underrun_and_recovery():
    jb = JitterBuffer(1000, target_s=0.1, max_s=0.5)
    out = jb.read(50)
    assert not out.any()  # still pre-buffering
    jb.write(np.ones(100, np.float32))
    out = jb.read(80)
    assert out[-1, 0] == 1.0 and out[0, 0] == 0.0  # fade-in starts at 0
    out = jb.read(80)  # only 20 left -> underrun
    assert jb.underruns == 1 and not out[20:].any()
    jb.write(np.ones(50, np.float32))
    assert not jb.read(10).any()  # waits until target (100) refilled - no stutter
    jb.write(np.ones(60, np.float32))
    assert jb.read(10).any()


def test_jitter_buffer_overrun_drops_to_target():
    jb = JitterBuffer(1000, target_s=0.1, max_s=0.3)
    jb.write(np.ones(250, np.float32))
    jb.write(np.ones(100, np.float32))
    assert jb.overruns == 1 and jb.fill == 100


def test_null_sink_consumes_in_real_time():
    s = NullSink(1000.0, latency_s=0.3)  # target 300 frames, overrun limit 600
    s.jitter.write(np.ones(580, np.float32))
    s.start()
    time.sleep(0.4)
    s.stop()
    played = s.jitter.frames_played
    assert 300 < played < 500 and s.jitter.overruns == 0


def test_simulated_retune_moves_signals():
    sim = SimulatedSource()
    sim.open()
    x, fc, fs = sim.generate_block()
    f1 = peak_frequency([sim.generate_block()[0] for _ in range(10)], fc, fs)
    sim.set_center_frequency(99.5e6)
    f2 = peak_frequency([sim.generate_block()[0] for _ in range(10)], 99.5e6, fs)
    # strongest signal (WFM at 99.8 MHz) stays at its absolute frequency
    assert abs(f1 - 99.8e6) < 30e3 and abs(f2 - 99.8e6) < 30e3


def test_hardware_sources_fail_cleanly_without_device():
    from pyrx.sources.hackrf_source import HackRFSource
    from pyrx.sources.rtlsdr_source import RtlSdrSource
    for cls in (RtlSdrSource, HackRFSource):
        ok, why = cls.available()
        assert isinstance(ok, bool) and isinstance(why, str)
        devs = cls.enumerate()
        if not ok:
            with pytest.raises(SourceError):
                cls().open()
        elif not devs:
            with pytest.raises(SourceError):
                cls().open()
