"""GUI tests (offscreen). Acceptance test 9 (smoke) is at the bottom."""

import os
import subprocess
import sys

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtGui, QtWidgets  # noqa: E402

from pyrx.dsp.spectrum import SpectrumFrame  # noqa: E402
from pyrx.ui.freq_display import FrequencyDisplay  # noqa: E402
from pyrx.ui.plots import FreqAxis, SpectrumPanel  # noqa: E402


@pytest.fixture
def fd(qtbot):
    w = FrequencyDisplay(10, 30)
    qtbot.addWidget(w)
    w.setRange(0, 6e9)
    w.setFrequency(100_000_000)
    w.show()
    return w


def digit_center(w, power):
    for p, r in w._digit_rects():
        if p == power:
            return r.center()


def test_freq_click_upper_and_lower_half(qtbot, fd):
    got = []
    fd.frequencyChanged.connect(got.append)
    c = digit_center(fd, 3)  # kHz digit
    qtbot.mouseClick(fd, QtCore.Qt.LeftButton, pos=QtCore.QPoint(c.x(), 3))
    assert fd.frequency() == 100_001_000
    qtbot.mouseClick(fd, QtCore.Qt.LeftButton, pos=QtCore.QPoint(c.x(), fd.height() - 3))
    qtbot.mouseClick(fd, QtCore.Qt.LeftButton, pos=QtCore.QPoint(c.x(), fd.height() - 3))
    assert fd.frequency() == 99_999_000
    assert got[-1] == 99_999_000


def test_freq_wheel_over_digit(qtbot, fd):
    c = digit_center(fd, 6)  # MHz digit
    ev = QtGui.QWheelEvent(QtCore.QPointF(c), QtCore.QPointF(fd.mapToGlobal(c)), QtCore.QPoint(0, 0),
                           QtCore.QPoint(0, 240), QtCore.Qt.NoButton, QtCore.Qt.NoModifier,
                           QtCore.Qt.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(fd, ev)
    assert fd.frequency() == 102_000_000


def test_freq_keyboard_entry(qtbot, fd):
    fd.setFocus()
    fd._cursor = 8  # hundreds of MHz... 100.000.000 -> digit '1'
    qtbot.keyClicks(fd, "145")
    assert fd.frequency() == 145_000_000
    qtbot.keyClick(fd, QtCore.Qt.Key_Up)  # cursor now on the 100 kHz digit
    assert fd.frequency() == 145_100_000


def test_freq_axis_labels_are_absolute():
    ax = FreqAxis("bottom")
    ax.ref = 100e6
    assert ax.tickStrings([0.0, 250e3], 1.0, 50e3) == ["100.00", "100.25"]
    levels = ax.tickValues(-1.2e6, 1.2e6, 1000)
    major = levels[0][1]
    # ticks fall on round absolute frequencies
    assert all(abs(((v + ax.ref) / levels[0][0]) - round((v + ax.ref) / levels[0][0])) < 1e-6 for v in major)


def make_frame(center=100e6, rate=2.4e6, n=8192, k_peak=None):
    db = np.full(n, -100.0, np.float32)
    if k_peak is not None:
        db[k_peak] = -10
    return SpectrumFrame(db, center, rate, 2.0)


def test_spectrum_peak_drawn_at_axis_frequency(qtbot):
    """Acceptance 6 (UI half): the bin of a tone is drawn where the axis says its frequency is."""
    sp = SpectrumPanel()
    qtbot.addWidget(sp)
    sp.resize(1200, 600)
    sp.show()
    sp.set_center(100e6)
    k = 8192 // 2 + 1024  # +300 kHz
    sp.set_frames([make_frame(k_peak=k)])
    xs, ys = sp.curve.getData()
    xpk = xs[int(np.argmax(ys))]
    f_axis = float(sp.freq_axis.label_for(xpk, 1.0)) * 1e6
    assert f_axis == pytest.approx(100.3e6, abs=2.4e6 / 8192 * 2)
    assert sp.x_to_freq(xpk) == pytest.approx(100.3e6, abs=2.4e6 / 8192 * 2)


def test_spectrum_click_and_drag_tuning(qtbot):
    sp = SpectrumPanel()
    qtbot.addWidget(sp)
    sp.resize(1200, 600)
    sp.show()
    sp.set_center(100e6)
    sp.set_vfo(100e6)
    sp.set_passband(-100e3, 100e3, "both")
    tuned, bws, drags = [], [], []
    sp.tuneRequested.connect(tuned.append)
    sp.bandwidthRequested.connect(bws.append)
    sp.vfoDragged.connect(drags.append)
    sp._on_click(500e3)
    assert tuned == [100.5e6]
    vb = sp.spec_vb
    edge_scene = vb.mapViewToScene(QtCore.QPointF(100e3, -50))
    assert sp._hit_test(vb, edge_scene) == "hi"
    sp._drag_start("hi", 100e3)
    sp._drag_move("hi", 150e3, True)
    assert bws[-1] == pytest.approx(300e3)
    inside = vb.mapViewToScene(QtCore.QPointF(20e3, -50))
    assert sp._hit_test(vb, inside) == "move"
    sp._drag_start("move", 20e3)
    sp._drag_move("move", 70e3, True)
    assert drags[-1] == pytest.approx(100.05e6)


def test_waterfall_shifts_on_retune(qtbot):
    sp = SpectrumPanel()
    qtbot.addWidget(sp)
    sp.show()
    n = 4096
    sp.set_frames([make_frame(100e6, 2.4e6, n, k_peak=n // 2)])
    col = int(np.argmax(sp.wf_item.hist[sp.wf_item.head].astype(np.float32)))
    sp.set_frames([make_frame(100.6e6, 2.4e6, n)])
    old_row = sp.wf_item.hist[(sp.wf_item.head + 1) % sp.wf_item.rows].astype(np.float32)
    # the old peak (at 100 MHz) is now 0.6 MHz left of the centre
    assert int(np.argmax(old_row)) == col - 1024


def test_main_window_basic_flow(qtbot, tmp_path):
    from pyrx.app import parse_args
    from pyrx.config.settings import Settings, set_config_dir
    from pyrx.ui.main_window import MainWindow
    set_config_dir(str(tmp_path))
    w = MainWindow(Settings(), parse_args(["--source", "simulated", "--no-audio"]))
    qtbot.addWidget(w)
    w.show()
    assert w.start()
    qtbot.waitUntil(lambda: w.frames_shown > 10, timeout=5000)
    w.set_mode("NFM")
    assert w.engine.params.mode == "NFM"
    w.set_vfo(100.25e6, snap_to_step=True)
    assert w.vfo == 100.25e6
    w.set_vfo(103e6)  # outside the span: centre follows
    assert w.source.center_frequency == 103e6
    w.step_tune(1)
    assert w.vfo == pytest.approx(103e6 + 12500)
    w.set_bandwidth(20000)
    assert w.engine.params.bandwidth == 20000
    w.p_rec.dir.setText(str(tmp_path))
    w._toggle_af_rec(True)
    qtbot.wait(700)
    w._toggle_af_rec(False)
    assert w.engine.af_rec.bytes_written > 0
    w.close()
    s = Settings(os.path.join(str(tmp_path), "settings.json")).load()
    assert s["radio"]["mode"] == "NFM" and s["radio"]["bandwidths"]["NFM"] == 20000
    assert s["ui"]["geometry"]


def test_smoke_command_line(tmp_path):
    """Acceptance 9: python -m pyrx --source simulated --autostart --exit-after 10."""
    # PYRX_NO_AUDIO: don't play sound on the test machine (the command line is the spec's)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYRX_CONFIG_DIR=str(tmp_path), PYRX_NO_AUDIO="1")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-m", "pyrx", "--source", "simulated", "--autostart", "--exit-after", "10"],
                       cwd=root, env=env, capture_output=True, text=True, timeout=60)
    log = p.stdout + p.stderr
    assert p.returncode == 0, log
    assert "Traceback" not in log and "CRITICAL" not in log and " ERROR " not in log, log
    assert "exit: ran" in log
    blocks = int(log.split(" blocks,")[0].split()[-1])
    assert blocks > 300, log  # ~50 blocks/s for ~9.8 s of streaming


# ----------------------------------------------------------------------------- more GUI flows

def test_device_forms_build_for_all_sources(qtbot):
    from pyrx.sources import source_classes
    from pyrx.ui.widgets import ControlsForm
    for cls in source_classes():
        src = cls()  # not opened: forms must still build
        f = ControlsForm(src)
        qtbot.addWidget(f)
        assert set(f.widgets) == {c.key for c in src.controls()}


def test_confirmation_required_for_antenna_power(qtbot, monkeypatch):
    from pyrx.sources.hackrf_source import HackRFSource
    from pyrx.ui.widgets import ControlsForm
    src = HackRFSource()
    f = ControlsForm(src)
    qtbot.addWidget(f)
    asked = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: asked.append(a[2]) or QtWidgets.QMessageBox.Cancel)
    f.widgets["antenna_power"].setChecked(True)
    assert asked and "3.3 V" in asked[0]
    assert src.get_control("antenna_power") is False
    assert not f.widgets["antenna_power"].isChecked()
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a, **k: QtWidgets.QMessageBox.Yes)
    f.widgets["antenna_power"].setChecked(True)
    assert src.get_control("antenna_power") is True  # stored (applied when a device is open)


def _window(qtbot, tmp_path, *extra):
    from pyrx.app import parse_args
    from pyrx.config.settings import Settings, set_config_dir
    from pyrx.ui.main_window import MainWindow
    set_config_dir(str(tmp_path))
    w = MainWindow(Settings(), parse_args(["--no-audio", *extra]))
    qtbot.addWidget(w)
    w.show()
    return w


def test_bookmark_tunes_frequency_mode_and_bandwidth(qtbot, tmp_path):
    from pyrx.config.bookmarks import Bookmark
    w = _window(qtbot, tmp_path, "--source", "simulated")
    w.p_bm.store.add(Bookmark("NFM sim", 100.25e6, "NFM", 10000, "Sim"))
    w.p_bm.reload()
    it = w.p_bm.tree.topLevelItem(0)
    w.p_bm.tree.itemDoubleClicked.emit(it, 0)
    assert (w.vfo, w.mode, w.engine.params.bandwidth) == (100.25e6, "NFM", 10000)
    assert w.p_radio.bandwidth.value() == 10000
    w.close()


def test_space_shortcut_and_mode_shortcut(qtbot, tmp_path):
    w = _window(qtbot, tmp_path, "--source", "simulated")
    QtWidgets.QApplication.setActiveWindow(w)
    qtbot.waitUntil(lambda: QtWidgets.QApplication.activeWindow() is w, timeout=2000)
    w.spectrum.spec_w.setFocus()
    qtbot.keyClick(w.spectrum.spec_w, QtCore.Qt.Key_Space)
    qtbot.waitUntil(lambda: w.engine.running, timeout=3000)
    qtbot.keyClick(w.spectrum.spec_w, QtCore.Qt.Key_2, QtCore.Qt.AltModifier)
    assert w.mode == "AM"
    vfo = w.vfo
    qtbot.keyClick(w.spectrum.spec_w, QtCore.Qt.Key_Right)
    assert w.vfo == pytest.approx(vfo + 5000)  # AM default step
    qtbot.keyClick(w.spectrum.spec_w, QtCore.Qt.Key_Space)
    qtbot.waitUntil(lambda: not w.engine.running, timeout=3000)
    w.close()


def test_iq_record_then_play_back_in_window(qtbot, tmp_path):
    import glob
    w = _window(qtbot, tmp_path, "--source", "simulated")
    w.p_rec.dir.setText(str(tmp_path))
    assert w.start()
    w._toggle_iq_rec(True)
    qtbot.wait(1500)
    w._toggle_iq_rec(False)
    rec = w.engine.iq_rec
    assert rec.bytes_written > 2.4e6 * 4 * 1.0 * 0.8  # ~1.5 s of 16-bit IQ
    w.stop()
    path = glob.glob(os.path.join(str(tmp_path), "*_IQ.wav"))[0]
    w.load_file(path)
    assert w.source.name == "file" and w.source.center_frequency == 100e6
    assert w.start()
    qtbot.waitUntil(lambda: w.frames_shown > 5, timeout=5000)
    assert w.engine.stats().dropped_blocks == 0
    w.close()


def test_unavailable_or_failing_source_shows_one_message(qtbot, tmp_path, monkeypatch):
    """A device that errors mid-stream: streaming stops and exactly one message is shown."""
    w = _window(qtbot, tmp_path, "--source", "simulated")
    assert w.start()
    qtbot.waitUntil(lambda: w.frames_shown > 3, timeout=5000)
    n0 = len(w.errors_shown)
    w.engine._on_source_error("Simulated unplug", "error")
    w.engine._on_source_error("Simulated unplug again", "error")
    qtbot.waitUntil(lambda: not w.engine.running, timeout=5000)
    qtbot.wait(300)
    assert w.errors_shown[n0:] == ["Simulated unplug"]
    assert not w.play.isChecked()
    w.close()
