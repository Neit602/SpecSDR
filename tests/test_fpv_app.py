"""FPV video in the application: scanner, IQ file round trip, audio subcarrier, GUI flows, smoke test."""

import glob
import os
import subprocess
import sys
import time

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtGui  # noqa: E402

from fpv_helpers import FS, PAL, decode, decoder_luma_response, hue_deg, luma_psnr, make_signal, read_counter  # noqa: E402
from pyrx.audio.sink import NullSink  # noqa: E402
from pyrx.config.fpv_channels import BANDS, nearest_channel, standard_channels  # noqa: E402
from pyrx.core.engine import Engine  # noqa: E402
from pyrx.core.fpv import FpvScanner  # noqa: E402
from pyrx.core.recorder import VideoRecorder, iq_recorder  # noqa: E402
from pyrx.dsp.fpv.pattern import VideoPattern  # noqa: E402
from pyrx.sources.iqfile import IqFileSource  # noqa: E402
from pyrx.sources.simulated import SimulatedSource  # noqa: E402
from tests_ui_helpers import open_window  # noqa: E402


def only_signals(sim, keep):
    for s in sim.signals:
        sim.set_control(f"{s.key}.enabled", s.key in keep)


# ----------------------------------------------------------------------------- scanner

def test_scanner_reports_exactly_the_two_video_channels():
    """Acceptance: 2 video transmitters, a CW carrier, a wideband noise-like signal -> 2 channels,
    correct standards. The sweep covers the whole 5.8 GHz table."""
    sim = SimulatedSource()
    sim.open()
    sim.set_sample_rate(FS)
    sim.set_center_frequency(5800e6)
    only_signals(sim, {"fpv_pal", "fpv_ntsc", "cw_58", "ofdm_58"})
    eng = Engine(NullSink())
    eng.set_source(sim)
    eng.start()
    try:
        sc = FpvScanner(eng, lambda f: nearest_channel(f))
        freqs = sorted({hz for _, hz in standard_channels()})
        done = []
        sc.on_finished = lambda: done.append(True)
        t0 = time.monotonic()
        sc.start(freqs)
        while not done and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        assert done, "scan did not finish"
        assert not sc.running and not sc.error
    finally:
        eng.stop()
    assert len(freqs) >= 40
    res = sorted(sc.results.values(), key=lambda r: r.frequency)
    assert len(res) == 2, res
    assert res[0].standard == "PAL" and abs(res[0].frequency - 5800e6) < 1e6
    assert res[1].standard == "NTSC" and abs(res[1].frequency - 5917e6) < 1e6
    assert res[0].channel in ("F4", "A4", "R5", "B5", "R4")
    assert res[1].channel == "R8"
    print(f"5.8 GHz table scan: {sc.last_duration:.1f} s for {len(freqs)} channels")


def test_channel_table_is_the_specified_one():
    assert BANDS["F"][3] == 5800 and BANDS["R"][7] == 5917 and BANDS["L"][0] == 5362
    assert BANDS["A"][7] == 5725 and BANDS["B"][0] == 5733 and BANDS["E"][4] == 5885
    assert len(standard_channels()) == 48
    assert nearest_channel(5917.2e6) == "R8"
    assert nearest_channel(1300e6) == ""


# ----------------------------------------------------------------------------- file round trip

def test_cs8_file_round_trip_decodes_the_pattern(tmp_path):
    """Acceptance: record 20 MS/s IQ as .cs8 (hackrf_transfer format) from the simulated
    transmitter, play it back through the file source, and decode the same test pattern."""
    sim = SimulatedSource()
    sim.open()
    sim.set_sample_rate(FS)
    sim.set_center_frequency(5800e6)
    only_signals(sim, {"fpv_pal"})
    rec = iq_recorder(str(tmp_path), sim.center_frequency, sim.sample_rate, "cs8")
    rec.start()
    n, target = 0, int(0.6 * FS)
    while n < target:
        x, _, _ = sim.generate_block()
        rec.push(x)
        n += len(x)
    rec.stop()
    assert rec.error is None and rec.dropped == 0
    files = glob.glob(os.path.join(str(tmp_path), "*.cs8"))
    assert len(files) == 1 and os.path.getsize(files[0]) == n * 2

    # hackrf_transfer compatibility: raw interleaved signed 8-bit I/Q
    raw = np.fromfile(files[0], dtype=np.int8, count=4)
    assert raw.dtype == np.int8

    src = IqFileSource()
    src.open(files[0])
    src.set_control("loop", False)
    assert src.center_frequency == 5800e6 and src.sample_rate == FS
    parts = []
    while True:
        y, eof = src.read_block(1 << 20)
        parts.append(y)
        if eof:
            break
    iq = np.concatenate(parts)
    assert len(iq) == n
    d, frames = decode(iq)
    assert d.stats.standard == "PAL" and d.stats.h_locked
    assert len(frames) >= 6
    pat = VideoPattern(PAL)
    counters = [read_counter(f.y, pat) for f in frames]
    assert all(b == a + 1 for a, b in zip(counters, counters[1:])), counters
    # picture correct: the luma matches the pattern
    ref = decoder_luma_response(pat.luma(counters[-1]), PAL)
    psnr, _ = luma_psnr(frames[-1].y, ref)
    assert psnr > 24, psnr


# ----------------------------------------------------------------------------- audio

def test_audio_subcarrier_recovers_tone():
    """FM subcarrier at 6.5 MHz carrying 1 kHz comes out as 1 kHz audio."""
    from helpers import dominant
    from pyrx.dsp.fpv.audio import FpvAudio
    fs = FS
    n = int(0.5 * fs)
    t = np.arange(n) / fs
    # subcarrier FM deviation 50 kHz, tone 1 kHz; mix into the demodulated video baseband
    beta = 50e3 / 1e3
    sub = 0.4e6 * np.cos(2 * np.pi * 6.5e6 * t + beta * np.sin(2 * np.pi * 1e3 * t))
    a = FpvAudio(fs, 48000.0, 6.5e6)
    a.enabled = True
    a.process(sub.astype(np.float32))
    out = a.take()
    assert len(out) > 10000
    fpk, pk, rest = dominant(out[3000:], 48000.0, exclude_hz=200.0)
    assert abs(fpk - 1000.0) < 5.0, fpk
    assert pk - rest > 25.0, (pk, rest)


def test_sim_transmitter_audio_reaches_decoder():
    """A simulated transmitter's 6.5 MHz FM sound comes out of the decoder's audio block."""
    from pyrx.core.fpv import FpvWorker
    from pyrx.dsp.fpv.decoder import FpvParams
    from helpers import dominant
    from pyrx.dsp.fpv.pattern import FpvTransmitter
    tx = FpvTransmitter(PAL, FS, audio_subcarrier=6.5e6)
    x = tx.generate(int(0.8 * FS))
    worker = FpvWorker(FS, FpvParams(), sink=NullSink())
    worker.audio.enabled = True
    try:
        while worker._warming:
            time.sleep(0.02)
        d = worker.decoder
        d.process(x)
        d.flush()
        out = worker.audio.take()
    finally:
        worker.stop()
    fpk, pk, rest = dominant(out[4800:], 48000.0, exclude_hz=200.0)
    assert abs(fpk - 1000.0) < 5.0, fpk
    assert pk - rest > 20.0, (pk, rest)


def test_audio_subcarrier_option_changes_frequency():
    from pyrx.dsp.fpv.audio import FpvAudio, SUBCARRIERS
    assert SUBCARRIERS == (6.5e6, 6.0e6, 5.5e6)
    a = FpvAudio(FS)
    a.set_subcarrier(5.5e6)
    assert a._mix.frequency == -5.5e6


# ----------------------------------------------------------------------------- other sample rates

@pytest.mark.parametrize("fs", [16e6, 10e6])
def test_decoder_handles_other_sample_rates(fs):
    from pyrx.dsp.fpv.decoder import FpvDecoder
    x, tx = make_signal(PAL, seconds=0.4, cnr=25, deviation=6e6, fs=fs)
    d = FpvDecoder(fs)
    d.process(x)
    d.flush()
    if fs >= 16e6:
        assert d.stats.h_locked and d.stats.v_locked and d.stats.standard == "PAL"
        frames = d.take_frames()
        assert frames
        c = [read_counter(f.y, tx.encoder.pattern) for f in frames]
        assert all(b == a + 1 for a, b in zip(c, c[1:])), c
    # below 16 MS/s the picture is degraded but nothing may raise


# ----------------------------------------------------------------------------- recorder

def test_video_recorder_writes_mp4(tmp_path):
    path = os.path.join(str(tmp_path), "x.mp4")
    rec = VideoRecorder(path, 720, 576, 25.0)
    rec.start()
    f = np.zeros((576, 720, 3), np.uint8)
    for i in range(15):
        f[:, :, 1] = i * 10
        rec.push(f.copy())
    rec.stop()
    assert rec.error is None and rec.frames_written == 15
    assert os.path.getsize(path) > 1000
    with open(path, "rb") as fh:
        head = fh.read(12)
    assert b"ftyp" in head  # an MP4 container


# ----------------------------------------------------------------------------- GUI

@pytest.fixture
def win(qtbot, tmp_path):
    w = open_window(qtbot, tmp_path, "--source", "simulated")
    yield w
    w.close()


def test_entering_and_leaving_fpv_mode_restores_settings(win, qtbot):
    src = win.source
    win.set_vfo(99.8e6)
    rate0, center0, vfo0 = src.sample_rate, src.center_frequency, win.vfo
    win.set_mode("WFM")
    win.set_mode("FPV")
    assert win.mode == "FPV"
    assert src.sample_rate == 20e6
    assert win.engine.fpv is not None
    assert src.center_frequency == win.p_fpv.current_channel()
    assert win.fpv_dock.isVisible()
    assert win.p_fpv.enable.isChecked()
    win.set_mode("WFM")
    assert win.engine.fpv is None
    assert src.sample_rate == rate0 and src.center_frequency == center0 and win.vfo == vfo0
    assert not win.fpv_dock.isVisible()


def test_fpv_decodes_in_the_window_and_snapshot(win, qtbot, tmp_path):
    win.p_rec.dir.setText(str(tmp_path))
    win.source.set_center_frequency(5800e6)
    win.set_mode("FPV")
    win.fpv_tune(5800e6)
    assert win.start()
    qtbot.waitUntil(lambda: win._fpv_last is not None, timeout=15000)
    qtbot.waitUntil(lambda: win.engine.fpv.status().standard == "PAL" and win.engine.fpv.status().v_locked, timeout=8000)
    path = win.fpv_snapshot()
    assert path and os.path.exists(path)
    win.stop()
    img = QtGui.QImage(path)
    assert img.width() == 720 and img.height() == 576


def test_fpv_mode_disabled_for_rtlsdr_with_explanation(win):
    win.select_source("rtlsdr")
    b = win.p_radio.mode_buttons["FPV"]
    assert not b.isEnabled()
    assert "RTL-SDR" in b.toolTip() and "bandwidth" in b.toolTip()
    assert not win.p_fpv.enable.isEnabled()


def test_channel_selector_and_bookmark_groups(win):
    from pyrx.config.fpv_channels import make_bookmark
    p = win.p_fpv
    assert p.band.count() == 6
    win.bookmarks.add(make_bookmark("Long range 1.3G", 1280e6, "1.3 GHz"))
    win.bookmarks.save()
    win._fpv_channels_changed()
    assert p.band.findData("user:1.3 GHz") >= 0
    assert p.band.count() == 7
    win.set_mode("FPV")
    # user channel: tune to it through the selector
    i = p.band.findData("user:1.3 GHz")
    p.band.setCurrentIndex(i)
    p._band_changed()
    assert win.vfo == 1280e6
    # and the scanner lists the group
    win.p_scan.reload_groups()
    items = [win.p_scan.bands.item(k).text() for k in range(win.p_scan.bands.count())]
    assert "1.3 GHz" in items


def test_scanner_results_double_click_tunes(win, qtbot):
    from pyrx.core.fpv import ScanResult
    win.set_mode("FPV")
    win.p_scan.set_results([ScanResult(5917e6, "R8", "NTSC", -20.0, time.time(), 0.9)])
    it = win.p_scan.results.topLevelItem(0)
    assert it.text(1) == "R8" and it.text(2) == "NTSC"
    win.p_scan.results.itemDoubleClicked.emit(it, 0)
    assert win.vfo == 5917e6 and win.source.center_frequency == 5917e6


def test_picture_controls_reach_the_decoder(win, qtbot):
    win.set_mode("FPV")
    dec = win.engine.fpv.decoder
    win.p_fpv.standard.setCurrentIndex(win.p_fpv.standard.findData("NTSC"))
    win.p_fpv.standard.activated.emit(win.p_fpv.standard.currentIndex())
    win.p_fpv.brightness.setValue(20)
    win.p_fpv.brightness.slider.valueChanged.emit(win.p_fpv.brightness.slider.value())
    qtbot.waitUntil(lambda: dec.params.standard == "NTSC" and dec.params.brightness == pytest.approx(0.2), timeout=2000)


def test_fpv_settings_persist(qtbot, tmp_path):
    w = open_window(qtbot, tmp_path, "--source", "simulated")
    w.set_mode("FPV")
    w.p_fpv.band.setCurrentIndex(w.p_fpv.band.findData("std:R"))
    w.p_fpv._band_changed()
    w.p_fpv.channel.setCurrentIndex(7)
    w.p_fpv._channel_chosen()
    assert w.vfo == 5917e6
    w.p_fpv.saturation.setValue(1.4)
    w.p_fpv.deinterlace.setCurrentIndex(1)
    w.p_scan.f_lo.setValue(1000)
    w.p_scan.f_hi.setValue(1400)
    w.close()
    from pyrx.config.settings import Settings
    s = Settings(os.path.join(str(tmp_path), "settings.json")).load()
    assert s["radio"]["mode"] == "FPV"
    assert s["fpv"]["saturation"] == pytest.approx(1.4)
    assert s["fpv"]["deinterlace"] == "double"
    assert s["fpv"]["scan"]["f_lo"] == 1000
    assert s["fpv"]["band"] == "std:R"
    w2 = open_window(qtbot, tmp_path, "--source", "simulated")
    assert w2.mode == "FPV"
    assert w2.p_fpv.deinterlace.currentData() == "double"
    assert w2.vfo == 5917e6
    w2.close()


def test_fpv_recording_through_window(win, qtbot, tmp_path):
    win.p_rec.dir.setText(str(tmp_path))
    win.source.set_center_frequency(5800e6)
    win.set_mode("FPV")
    win.fpv_tune(5800e6)
    assert win.start()
    qtbot.waitUntil(lambda: win._fpv_last is not None, timeout=15000)
    win.p_fpv.record_btn.setChecked(True)
    qtbot.wait(800)
    win.p_fpv.record_btn.setChecked(False)
    win.stop()
    files = glob.glob(os.path.join(str(tmp_path), "*_FPV.mp4"))
    assert files and os.path.getsize(files[0]) > 1000
    assert "5800000000Hz" in os.path.basename(files[0])


def test_video_smoke_command_line(tmp_path):
    """Acceptance: python -m pyrx --source simulated --mode fpv --frequency 5800e6 --autostart
    --exit-after 10 --snapshot out.png exits 0, logs no exceptions and writes the test pattern."""
    out = os.path.join(str(tmp_path), "out.png")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYRX_CONFIG_DIR=str(tmp_path), PYRX_NO_AUDIO="1")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-m", "pyrx", "--source", "simulated", "--mode", "fpv", "--frequency", "5800e6",
                        "--autostart", "--exit-after", "10", "--snapshot", out],
                       cwd=root, env=env, capture_output=True, text=True, timeout=90)
    log = p.stdout + p.stderr
    assert p.returncode == 0, log
    assert "Traceback" not in log and "CRITICAL" not in log and " ERROR " not in log, log
    assert os.path.exists(out)
    img = QtGui.QImage(out)
    assert img.width() == 720 and img.height() == 576
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(576, 720, -1)
    rgb = arr[:, :, [2, 1, 0]] if img.format() == QtGui.QImage.Format_RGB32 else arr[:, :, :3]
    pat = VideoPattern(PAL)
    # colour bars of the test pattern
    r0, r1 = pat.band["BARS"] + 8, pat.band["GREY"] - 8
    for i in (1, 2, 5, 6):
        x0, x1 = pat.bar_edges[i] + 12, pat.bar_edges[i + 1] - 12
        c = np.mean(rgb[r0:r1, x0:x1], axis=(0, 1))
        h, hs = hue_deg(*c), hue_deg(*pat.bar_rgb[i])
        assert abs((h - hs + 180) % 360 - 180) < 12, (i, c)
    # greyscale steps
    g0, g1 = pat.band["GREY"] + 8, pat.band["WEDGE"] - 8
    grey = np.mean(rgb[g0:g1, 30:80], axis=(0, 2)).mean() / 255, np.mean(rgb[g0:g1, 620:680], axis=(0, 2)).mean() / 255
    assert grey[0] < 0.1 and grey[1] > 0.85
