"""Main window and the controller logic that ties UI, engine and sources together."""

from __future__ import annotations

import base64
import logging
import math
import os
import time

from PyQt5 import QtCore, QtGui, QtWidgets

from ..audio.sink import SoundDeviceSink, create_sink, sounddevice_available
from ..config.bookmarks import Bookmark, BookmarkStore
from ..config.settings import Settings, default_recordings_dir
from ..core.engine import Engine
from ..core.tuning import TuneContext, plan, snap
from ..dsp.receiver import MODE_INFO, MODES, passband
from ..dsp.spectrum import noise_floor_db
from ..sources import SourceError, source_classes
from ..sources.iqfile import FileMetadataMissing, IqFileSource
from .freq_display import FrequencyDisplay, parse_frequency
from .panels import (AgcPanel, AudioPanel, BookmarksPanel, DisplayPanel, RadioPanel, RecordingPanel,
                     SourcePanel, fmt_rate, fmt_size, fmt_time)
from .plots import SpectrumPanel
from .widgets import DeviceDialog, SidePanel

log = logging.getLogger(__name__)

GUI_FPS_CAP = 60
# The engine computes spectra at FRAME_OVERSAMPLE x the display rate so that every GUI tick
# finds a fresh frame (otherwise block-quantised arrival aliases against the timer).
FRAME_OVERSAMPLE = 2.0

SHORTCUTS = [
    ("Space", "Start / stop streaming"),
    ("Left / Right", "Tune VFO down / up one step"),
    ("Shift+Left / Shift+Right", "Tune ten steps"),
    ("Up / Down", "Volume up / down"),
    ("M", "Mute"),
    ("Ctrl++ / Ctrl+-", "Zoom spectrum in / out"),
    ("Ctrl+wheel", "Zoom around the mouse"),
    ("Mouse wheel", "Tune by one step"),
    ("Right/middle drag", "Pan the zoomed spectrum"),
    ("Alt+1 … Alt+8", "Mode: " + ", ".join(MODES)),
    ("S", "Squelch on/off"),
    ("A", "Auto levels"),
    ("Ctrl+F", "Enter a frequency"),
    ("Ctrl+B", "Bookmark current frequency"),
    ("Ctrl+R", "Record audio on/off"),
    ("Ctrl+Shift+R", "Record baseband IQ on/off"),
    ("Ctrl+L", "Show / hide side panel"),
    ("F11", "Full screen"),
    ("Ctrl+Q", "Quit"),
]


def _b64(qba: QtCore.QByteArray) -> str:
    return base64.b64encode(bytes(qba)).decode()


def _qba(s: str) -> QtCore.QByteArray:
    try:
        return QtCore.QByteArray(base64.b64decode(s)) if s else QtCore.QByteArray()
    except ValueError:
        return QtCore.QByteArray()


class VSlider(QtWidgets.QWidget):
    """Small labelled vertical slider for the right-hand column."""

    valueChanged = QtCore.pyqtSignal(int)

    def __init__(self, title, lo, hi, value, tooltip):
        super().__init__()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(2)
        self.s = QtWidgets.QSlider(QtCore.Qt.Vertical)
        self.s.setRange(lo, hi)
        self.s.setValue(value)
        self.s.setToolTip(tooltip)
        lab = QtWidgets.QLabel(title)
        lab.setAlignment(QtCore.Qt.AlignCenter)
        lab.setStyleSheet("color:#8a8f98;font-size:8pt;")
        lay.addWidget(self.s, 1, QtCore.Qt.AlignHCenter)
        lay.addWidget(lab)
        self.s.valueChanged.connect(self.valueChanged)

    def value(self):
        return self.s.value()

    def setValue(self, v):
        self.s.blockSignals(True)
        self.s.setValue(int(round(v)))
        self.s.blockSignals(False)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, settings: Settings, args=None):
        super().__init__()
        self.settings = settings
        self.args = args
        self.setWindowTitle("PyRX")
        self.bookmarks = BookmarkStore().load()
        self._sources: dict[str, object] = {}
        self.source = None
        self.vfo = 100e6
        self.mode = "WFM"
        self.bandwidths: dict[str, float] = {}
        self.orders: dict[str, int] = {}
        self.step = 0.0
        self.snap_on = True
        self.lo_offset, self.lo_offset_hz = False, 250e3
        self._dialog_open = False
        self._cpu_t = (time.monotonic(), time.process_time())
        self.cpu = 0.0
        self.frames_shown = 0
        self._dev_dialog: DeviceDialog | None = None
        self._was_running = False
        self.errors_shown: list[str] = []

        no_audio = bool(args and getattr(args, "no_audio", False)) or os.environ.get("PYRX_NO_AUDIO") == "1"
        a = settings["audio"]
        self.sink, msg = create_sink(a.get("device"), a.get("latency_ms", 120) / 1000.0, prefer_null=no_audio)
        self._audio_msg = msg
        self.engine = Engine(self.sink)

        self._build_ui()
        self._build_shortcuts()
        self._restore()

        self._gui_timer = QtCore.QTimer(self)
        self._gui_timer.setTimerType(QtCore.Qt.PreciseTimer)
        self._gui_timer.timeout.connect(self._on_gui_tick)
        self._gui_timer.start(int(1000 / self.p_display.fps.value()))
        self._status_timer = QtCore.QTimer(self)
        self._status_timer.timeout.connect(self._on_status_tick)
        self._status_timer.start(250)
        self._wf_recolor_timer = QtCore.QTimer(self)
        self._wf_recolor_timer.setSingleShot(True)
        if msg:
            QtCore.QTimer.singleShot(0, lambda: self.statusBar().showMessage(msg, 10000))
            log.warning(msg)

    # ======================================================================= UI
    def _build_ui(self):
        st = self.style()
        tb = QtWidgets.QToolBar("Main")
        tb.setObjectName("maintoolbar")
        tb.setMovable(False)
        tb.setIconSize(QtCore.QSize(20, 20))
        self.addToolBar(tb)
        self.toggle_side = QtWidgets.QToolButton()
        self.toggle_side.setText("☰")
        self.toggle_side.setCheckable(True)
        self.toggle_side.setChecked(True)
        self.toggle_side.setToolTip("Show / hide side panel (Ctrl+L)")
        tb.addWidget(self.toggle_side)
        self.play = QtWidgets.QToolButton()
        self.play.setCheckable(True)
        self.play.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.play.setToolTip("Start / stop (Space)")
        tb.addWidget(self.play)
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.setToolTip("Signal source")
        for cls in source_classes():
            ok, why = cls.available()
            self.source_combo.addItem(cls.display_name if ok else f"{cls.display_name} (unavailable)", cls.name)
            if not ok:
                idx = self.source_combo.count() - 1
                self.source_combo.setItemData(idx, why, QtCore.Qt.ToolTipRole)
        tb.addWidget(self.source_combo)
        self.config_btn = QtWidgets.QToolButton()
        self.config_btn.setText("⚙")
        self.config_btn.setToolTip("Device settings")
        tb.addWidget(self.config_btn)
        tb.addSeparator()
        self.freq = FrequencyDisplay(10, 34)
        self.freq.setToolTip("VFO frequency. " + self.freq.toolTip())
        tb.addWidget(self.freq)
        tb.addSeparator()
        cbox = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(cbox)
        cl.setContentsMargins(4, 0, 4, 0)
        cl.setSpacing(0)
        lab = QtWidgets.QLabel("Centre")
        lab.setStyleSheet("color:#8a8f98;font-size:8pt;")
        cl.addWidget(lab)
        self.center_disp = FrequencyDisplay(10, 15)
        self.center_disp.setToolTip("Hardware centre frequency (tunable sources only). " + self.center_disp.toolTip())
        cl.addWidget(self.center_disp)
        tb.addWidget(cbox)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.level_bar = QtWidgets.QProgressBar()
        self.level_bar.setRange(-120, 0)
        self.level_bar.setTextVisible(True)
        self.level_bar.setFormat("%v dBFS")
        self.level_bar.setFixedWidth(130)
        self.level_bar.setToolTip("Signal level in the VFO passband")
        tb.addWidget(self.level_bar)
        tb.addSeparator()
        self.mute = QtWidgets.QToolButton()
        self.mute.setCheckable(True)
        self.mute.setIcon(st.standardIcon(QtWidgets.QStyle.SP_MediaVolume))
        self.mute.setToolTip("Mute (M)")
        tb.addWidget(self.mute)
        self.volume = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(130)
        self.volume.setToolTip("Volume (Up/Down)")
        tb.addWidget(self.volume)

        # ---- side panel
        self.side = SidePanel()
        self.p_source = SourcePanel()
        self.p_radio = RadioPanel()
        self.p_agc = AgcPanel()
        self.p_audio = AudioPanel()
        self.p_display = DisplayPanel()
        self.p_rec = RecordingPanel()
        self.p_bm = BookmarksPanel(self.bookmarks)
        self.side.add_section("source", "Source", self.p_source)
        self.side.add_section("radio", "Radio", self.p_radio)
        self.side.add_section("agc", "AGC", self.p_agc, False)
        self.side.add_section("audio", "Audio", self.p_audio, False)
        self.side.add_section("display", "Display", self.p_display, False)
        self.side.add_section("recording", "Recording", self.p_rec, False)
        self.side.add_section("bookmarks", "Frequency manager", self.p_bm, False)

        # ---- main area
        self.spectrum = SpectrumPanel()
        right = QtWidgets.QWidget()
        rl = QtWidgets.QVBoxLayout(right)
        rl.setContentsMargins(0, 4, 0, 4)
        self.s_zoom = VSlider("Zoom", 0, 100, 0, "Zoom (Ctrl+wheel on the spectrum)")
        self.s_contrast = VSlider("Contr.", -100, 100, 0, "Waterfall contrast")
        self.s_range = VSlider("Range", 10, 200, 110, "Spectrum / waterfall dB range")
        self.s_offset = VSlider("Ref", -150, 30, 0, "Reference level (top of the scale), dBFS")
        for s in (self.s_zoom, self.s_contrast, self.s_range, self.s_offset):
            rl.addWidget(s, 1)
        right.setFixedWidth(48)
        mainw = QtWidgets.QWidget()
        ml = QtWidgets.QHBoxLayout(mainw)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        ml.addWidget(self.spectrum, 1)
        ml.addWidget(right)
        self.main_split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.main_split.addWidget(self.side)
        self.main_split.addWidget(mainw)
        self.main_split.setStretchFactor(0, 0)
        self.main_split.setStretchFactor(1, 1)
        self.main_split.setSizes([300, 1100])
        self.setCentralWidget(self.main_split)

        # ---- status bar
        sb = self.statusBar()
        self.st_level = QtWidgets.QLabel()
        self.st_snr = QtWidgets.QLabel()
        self.st_rate = QtWidgets.QLabel()
        self.st_drop = QtWidgets.QLabel()
        self.st_audio = QtWidgets.QLabel()
        self.st_cpu = QtWidgets.QLabel()
        self.st_fps = QtWidgets.QLabel()
        self.st_level.setToolTip("Channel power inside the VFO passband")
        self.st_snr.setToolTip("Channel power relative to the noise floor estimated from the spectrum")
        self.st_rate.setToolTip("Sample rate actually arriving from the source (nominal in brackets)")
        self.st_drop.setToolTip("Blocks lost: engine queue full + reported by the source/driver")
        self.st_audio.setToolTip("Audio buffer fill, underruns / overruns, clock-drift correction")
        self.st_cpu.setToolTip("CPU used by PyRX in % of one core (DSP thread load in brackets)")
        for w in (self.st_level, self.st_snr, self.st_rate, self.st_drop, self.st_audio, self.st_cpu, self.st_fps):
            sb.addPermanentWidget(w)

        # ---- menu
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction("Open IQ file…", self.open_file_dialog)
        m.addSeparator()
        m.addAction("Quit", self.close, QtGui.QKeySequence("Ctrl+Q"))
        m = mb.addMenu("&View")
        a = m.addAction("Side panel", lambda: self.toggle_side.toggle())
        a.setShortcut(QtGui.QKeySequence("Ctrl+L"))
        a = m.addAction("Full screen", self._toggle_fullscreen)
        a.setShortcut(QtGui.QKeySequence("F11"))
        m = mb.addMenu("&Help")
        m.addAction("Keyboard shortcuts", self._show_shortcuts)
        m.addAction("About PyRX", self._about)

        # ---- wiring
        self.toggle_side.toggled.connect(self.side.setVisible)
        self.play.toggled.connect(self._on_play_toggled)
        self.source_combo.activated.connect(lambda i: self.select_source(self.source_combo.itemData(i)))
        self.config_btn.clicked.connect(self.open_device_dialog)
        self.freq.frequencyChanged.connect(lambda hz: self.set_vfo(hz, snap_to_step=False))
        self.center_disp.frequencyChanged.connect(self.set_center)
        self.volume.valueChanged.connect(self._set_volume)
        self.mute.toggled.connect(self._set_mute)

        ps = self.p_source
        ps.deviceChanged.connect(self._on_device_changed)
        ps.refreshRequested.connect(self._refresh_devices)
        ps.sampleRateChanged.connect(self.set_sample_rate)
        ps.openFileRequested.connect(self.open_file_dialog)
        ps.seekRequested.connect(self._seek)
        ps.dcChanged.connect(self.engine.set_dc_removal)
        ps.loOffsetChanged.connect(self._set_lo_offset)
        ps.swapIqChanged.connect(self.engine.set_swap_iq)
        ps.controlChanged.connect(self._on_control_changed)

        pr = self.p_radio
        pr.modeChanged.connect(self.set_mode)
        pr.bandwidthChanged.connect(self.set_bandwidth)
        pr.filterChanged.connect(self._set_filter)
        pr.stepChanged.connect(self._set_step)
        pr.snapChanged.connect(lambda on: setattr(self, "snap_on", on))
        pr.squelchChanged.connect(lambda on, thr: self.engine.set_squelch(on, thr))
        pr.deemphasisChanged.connect(self.engine.set_deemphasis)
        pr.stereoChanged.connect(self.engine.set_stereo)
        pr.cwPitchChanged.connect(self.engine.set_cw_pitch)
        self.p_agc.agcChanged.connect(lambda d: self.engine.set_agc(**d))
        self.p_audio.deviceChanged.connect(self._set_audio_device)
        self.p_audio.latencyChanged.connect(lambda ms: self.engine.sink.jitter.set_latency(ms / 1000.0))
        self.p_display.fftChanged.connect(self._set_fft)
        self.p_display.viewChanged.connect(self._set_view)
        self.p_display.autoLevels.connect(self.auto_levels)
        self.p_rec.iqToggled.connect(self._toggle_iq_rec)
        self.p_rec.audioToggled.connect(self._toggle_af_rec)
        self.p_rec.directoryChanged.connect(lambda d: self.settings["recording"].__setitem__("directory", d))
        self.p_rec.formatChanged.connect(lambda f: self.settings["recording"].__setitem__("iq_format", f))
        self.p_bm.tuneRequested.connect(self.tune_bookmark)
        self.p_bm.addRequested.connect(self.add_bookmark)

        sp = self.spectrum
        sp.tuneRequested.connect(lambda hz: self.set_vfo(hz, snap_to_step=self.snap_on, follow=False))
        sp.vfoDragged.connect(lambda hz: self.set_vfo(hz, snap_to_step=self.snap_on, follow=False))
        sp.bandwidthRequested.connect(self._drag_bandwidth)
        sp.wheelTune.connect(lambda n: self.step_tune(n))
        sp.zoomChanged.connect(self._zoom_changed)
        self.s_zoom.valueChanged.connect(self._zoom_slider)
        self.s_contrast.valueChanged.connect(lambda v: self.spectrum.set_wf_contrast(v))
        self.s_range.valueChanged.connect(lambda _: self._levels_changed())
        self.s_offset.valueChanged.connect(lambda _: self._levels_changed())
        self._set_play_button(False)

    def _build_shortcuts(self):
        def sc(key, fn):
            s = QtWidgets.QShortcut(QtGui.QKeySequence(key), self)
            s.setContext(QtCore.Qt.WindowShortcut)
            s.activated.connect(fn)
            return s

        sc("Space", lambda: self.play.toggle())
        sc("Left", lambda: self.step_tune(-1))
        sc("Right", lambda: self.step_tune(1))
        sc("Shift+Left", lambda: self.step_tune(-10))
        sc("Shift+Right", lambda: self.step_tune(10))
        sc("Up", lambda: self.volume.setValue(self.volume.value() + 5))
        sc("Down", lambda: self.volume.setValue(self.volume.value() - 5))
        sc("M", lambda: self.mute.toggle())
        sc("S", lambda: self.p_radio.sq.toggle())
        sc("A", self.auto_levels)
        sc("Ctrl++", lambda: self.spectrum.set_zoom(self.spectrum.zoom * 1.5))
        sc("Ctrl+=", lambda: self.spectrum.set_zoom(self.spectrum.zoom * 1.5))
        sc("Ctrl+-", lambda: self.spectrum.set_zoom(self.spectrum.zoom / 1.5))
        sc("Ctrl+F", self.freq.directEntry)
        sc("Ctrl+B", self.add_bookmark)
        sc("Ctrl+R", lambda: self.p_rec.af_btn.toggle())
        sc("Ctrl+Shift+R", lambda: self.p_rec.iq_btn.toggle())
        for i, m in enumerate(MODES):
            sc(f"Alt+{i + 1}", lambda m=m: self.set_mode(m))

    # ======================================================================= settings
    def _restore(self):
        s = self.settings
        src, radio, disp, ui = s["source"], s["radio"], s["display"], s["ui"]
        self.bandwidths = {k: float(v) for k, v in radio.get("bandwidths", {}).items() if k in MODES}
        self.orders = {k: int(v) for k, v in radio.get("orders", {}).items() if k in MODES}
        self.step = float(radio.get("step", 0) or 0)
        self.snap_on = bool(radio.get("snap", True))
        self.lo_offset = bool(src.get("lo_offset", False))
        self.lo_offset_hz = float(src.get("lo_offset_hz", 250e3))

        pr = self.p_radio
        pr.window.setCurrentIndex(max(0, pr.window.findData(radio.get("filter_window", "blackmanharris"))))
        pr.step.setCurrentIndex(max(0, pr.step.findData(self.step)))
        pr.snap.setChecked(self.snap_on)
        pr.sq.blockSignals(True)
        pr.sq.setChecked(bool(radio.get("squelch")))
        pr.sq.blockSignals(False)
        pr.sq_thr.setValue(radio.get("squelch_db", -60))
        tau = float(radio.get("deemphasis_us", 50)) * 1e-6
        pr.deemph.setCurrentIndex(max(0, pr.deemph.findData(tau)))
        pr.stereo.blockSignals(True)
        pr.stereo.setChecked(bool(radio.get("stereo", True)))
        pr.stereo.blockSignals(False)
        pr.cw.blockSignals(True)
        pr.cw.setValue(int(radio.get("cw_pitch", 600)))
        pr.cw.blockSignals(False)
        pr.set_stereo_indicator(False)
        e = self.engine
        e.set_squelch(pr.sq.isChecked(), pr.sq_thr.value())
        e.set_deemphasis(tau)
        e.set_stereo(pr.stereo.isChecked())
        e.set_cw_pitch(float(pr.cw.value()))
        e.params.filter_window = pr.window.currentData()
        self.p_agc.set_values(s["agc"])
        e.set_agc(**self.p_agc.values())

        a = s["audio"]
        self.p_audio.set_devices(SoundDeviceSink.output_devices(), a.get("device", "default"))
        self.p_audio.latency.blockSignals(True)
        self.p_audio.latency.setValue(int(a.get("latency_ms", 120)))
        self.p_audio.latency.blockSignals(False)
        self.volume.setValue(int(a.get("volume", 50)))
        self._set_volume(self.volume.value())
        self.mute.setChecked(bool(a.get("muted", False)))

        self.p_display.set_values(disp)
        e.set_fft(fft_size=int(disp["fft_size"]), window=disp["window"],
                  frame_rate=min(float(disp["fps"]), GUI_FPS_CAP) * FRAME_OVERSAMPLE,
                  averaging=int(disp["averaging"]), waterfall_rate=float(disp["wf_speed"]))
        sp = self.spectrum
        sp.set_smoothing(float(disp["smoothing"]))
        sp.set_peak_hold(bool(disp["peak_hold"]))
        sp.set_fill(bool(disp["fill"]))
        sp.set_colormap(disp["colormap"])
        self.s_range.setValue(disp["range_db"])
        self.s_offset.setValue(disp["ref_db"])
        self.s_contrast.setValue(disp["wf_contrast"])
        sp.set_wf_contrast(float(disp["wf_contrast"]))
        self._levels_changed()
        if float(disp.get("zoom", 1.0)) > 1.0:
            QtCore.QTimer.singleShot(0, lambda z=float(disp["zoom"]): self.spectrum.set_zoom(z))

        rec = s["recording"]
        self.p_rec.dir.setText(rec.get("directory") or default_recordings_dir())
        self.p_rec.fmt.setCurrentIndex(max(0, self.p_rec.fmt.findData(rec.get("iq_format", "cs16"))))

        self.p_source.dc.setChecked(bool(src.get("dc_removal", True)))
        e.set_dc_removal(self.p_source.dc.isChecked())
        self.p_source.swap.setChecked(bool(src.get("swap_iq", False)))
        self.p_source.lo.blockSignals(True)
        self.p_source.lo.setChecked(self.lo_offset)
        self.p_source.lo.blockSignals(False)
        self.p_source.lo_hz.blockSignals(True)
        self.p_source.lo_hz.setValue(int(self.lo_offset_hz / 1e3))
        self.p_source.lo_hz.blockSignals(False)

        if ui.get("geometry"):
            self.restoreGeometry(_qba(ui["geometry"]))
        else:
            self.resize(1400, 860)
        if ui.get("state"):
            self.restoreState(_qba(ui["state"]))
        if ui.get("main_splitter"):
            self.main_split.restoreState(_qba(ui["main_splitter"]))
        if ui.get("plot_splitter"):
            self.spectrum.splitter.restoreState(_qba(ui["plot_splitter"]))
        self.toggle_side.setChecked(bool(ui.get("side_panel", True)))
        for k, on in ui.get("sections", {}).items():
            if k in self.side.sections:
                self.side.sections[k].set_expanded(bool(on))

        # source + tuning (command line wins)
        args = self.args
        name = (args.source if args and args.source else None) or src.get("name", "simulated")
        if name not in [self.source_combo.itemData(i) for i in range(self.source_combo.count())]:
            name = "simulated"
        mode = radio.get("mode", "WFM") if radio.get("mode") in MODES else "WFM"
        if args and getattr(args, "mode", None):
            mode = args.mode.upper()
        self.mode = mode
        self.select_source(name, initial=True)
        vfo = float(radio.get("vfo", 99.8e6))
        if args and getattr(args, "freq", None):
            vfo = float(args.freq)
        self.set_mode(mode, force=True)
        if self.source is not None and self.source.tunable:
            self.set_vfo(vfo, snap_to_step=False)
        else:
            self.set_vfo(self.source.center_frequency if self.source else vfo, snap_to_step=False)

    def save_settings(self):
        s = self.settings
        src, radio, disp, ui, a = s["source"], s["radio"], s["display"], s["ui"], s["audio"]
        if self.source is not None:
            src["name"] = self.source.name
            src["sample_rate"] = self.source.sample_rate
            if self.source.tunable:
                src["center"] = self.source.center_frequency
            src.setdefault("controls", {})[self.source.name] = {
                k: v for k, v in self.source.control_state().items()
                if isinstance(v, (int, float, str, bool)) or v is None}
            if isinstance(self.source, IqFileSource) and self.source.path:
                src["file"] = self.source.path
            dev = self.p_source.device.currentData()
            if dev:
                src.setdefault("devices", {})[self.source.name] = dev
        src["dc_removal"] = self.p_source.dc.isChecked()
        src["swap_iq"] = self.p_source.swap.isChecked()
        src["lo_offset"] = self.lo_offset
        src["lo_offset_hz"] = self.lo_offset_hz
        radio["vfo"] = self.vfo
        radio["mode"] = self.mode
        self.bandwidths[self.mode] = self.engine.params.bandwidth or MODE_INFO[self.mode].bandwidth
        radio["bandwidths"] = self.bandwidths
        radio["orders"] = self.orders
        radio["filter_window"] = self.p_radio.window.currentData()
        radio["step"] = self.step
        radio["snap"] = self.snap_on
        radio["squelch"] = self.p_radio.sq.isChecked()
        radio["squelch_db"] = self.p_radio.sq_thr.value()
        radio["deemphasis_us"] = float(self.p_radio.deemph.currentData()) * 1e6
        radio["stereo"] = self.p_radio.stereo.isChecked()
        radio["cw_pitch"] = float(self.p_radio.cw.value())
        s.data["agc"] = self.p_agc.values()
        a["device"] = self.p_audio.device.currentData()
        a["volume"] = self.volume.value()
        a["muted"] = self.mute.isChecked()
        a["latency_ms"] = self.p_audio.latency.value()
        pd = self.p_display
        disp.update(fft_size=pd.size.currentData(), window=pd.window.currentData(), fps=pd.fps.value(),
                    averaging=pd.averaging.value(), smoothing=pd.smoothing.value() / 100.0,
                    peak_hold=pd.peak.isChecked(), fill=pd.fill.isChecked(), wf_speed=pd.speed.value(),
                    colormap=pd.cmap.currentData(), range_db=self.s_range.value(), ref_db=self.s_offset.value(),
                    wf_contrast=self.s_contrast.value(), zoom=self.spectrum.zoom)
        s["recording"]["directory"] = self.p_rec.dir.text()
        ui["geometry"] = _b64(self.saveGeometry())
        ui["state"] = _b64(self.saveState())
        ui["main_splitter"] = _b64(self.main_split.saveState())
        ui["plot_splitter"] = _b64(self.spectrum.splitter.saveState())
        ui["side_panel"] = self.toggle_side.isChecked()
        ui["sections"] = {k: sec.is_expanded() for k, sec in self.side.sections.items()}
        try:
            s.save()
        except OSError as e:
            log.error("could not save settings: %s", e)

    # ======================================================================= sources
    def _source_instance(self, name):
        if name not in self._sources:
            cls = next(c for c in source_classes() if c.name == name)
            src = cls()
            saved = self.settings["source"]
            ctrl = saved.get("controls", {}).get(name, {})
            src.restore_controls(ctrl)
            if saved.get("name") == name:
                try:
                    if src.tunable:
                        src._center = float(saved.get("center", src.center_frequency))
                    if saved.get("sample_rate"):
                        rates = src.sample_rates()
                        rng = src.sample_rate_range()
                        r = float(saved["sample_rate"])
                        if (rng and rng[0] <= r <= rng[1]) or r in rates:
                            src._rate = r
                except (TypeError, ValueError):
                    pass
            if isinstance(src, IqFileSource):
                src.path = (self.args.file if self.args and getattr(self.args, "file", None) else None) or \
                    saved.get("file") or None
            self._sources[name] = src
        return self._sources[name]

    def select_source(self, name: str, initial: bool = False):
        if self.source is not None and self.source.name == name and not initial:
            return
        was_running = self.engine.running
        self.stop()
        if self.source is not None and self.source.is_open:
            self.engine._call_with_timeout(self.source.close, 3.0, "closing the device")
        cls = next(c for c in source_classes() if c.name == name)
        ok, why = cls.available()
        i = self.source_combo.findData(name)
        self.source_combo.setCurrentIndex(i)
        self.source = self._source_instance(name)
        self.engine.set_source(self.source)
        self.p_source.info.setText("" if ok else why)
        self._refresh_devices(open_now=ok)
        if was_running and not initial and ok:
            self.start()

    def _refresh_devices(self, open_now: bool = True):
        src = self.source
        is_file = isinstance(src, IqFileSource)
        devs = [] if is_file else src.enumerate()
        cur = self.settings["source"].get("devices", {}).get(src.name)
        if cur not in [d.id for d in devs]:
            cur = devs[0].id if devs else None
        self.p_source.set_devices(devs, cur, is_file)
        if open_now and (devs or (is_file and src.path)) and not src.is_open:
            self._open_source(cur, quiet=True)
        self._source_changed_ui()

    def _on_device_changed(self, dev_id: str):
        src = self.source
        was = self.engine.running
        self.stop()
        if src.is_open:
            self.engine._call_with_timeout(src.close, 3.0, "closing the device")
        self.settings["source"].setdefault("devices", {})[src.name] = dev_id
        if self._open_source(dev_id) and was:
            self.start()
        self._source_changed_ui()

    def _open_source(self, dev_id=None, quiet=False) -> bool:
        src = self.source
        try:
            if isinstance(src, IqFileSource):
                src.open(src.path)
            else:
                src.open(dev_id if dev_id is not None else self.p_source.device.currentData())
            self.p_source.info.setText(src.info_text())
            return True
        except FileMetadataMissing as e:
            if quiet:
                self.p_source.info.setText(str(e))
                return False
            if self._ask_file_metadata(e):
                return self._open_source(dev_id, quiet)
            return False
        except SourceError as e:
            self.p_source.info.setText(str(e))
            if not quiet:
                self._error_box("Cannot open source", str(e))
            return False
        except Exception as e:  # driver threw something unexpected
            log.exception("opening source")
            self.p_source.info.setText(str(e))
            if not quiet:
                self._error_box("Cannot open source", f"{type(e).__name__}: {e}")
            return False

    def _source_changed_ui(self, rebuild_forms: bool = True):
        src = self.source
        if rebuild_forms:
            self.p_source.set_source(src)
        fixed = not src.tunable or src.sample_rate_range() is None and len(src.sample_rates()) <= 1
        self.p_source.set_rates(src.sample_rates(), src.sample_rate, fixed)
        self.p_source.dc.setEnabled(True)
        self.p_source.lo.setEnabled(src.tunable)
        self.p_source.lo_hz.setEnabled(src.tunable)
        if src.is_open:
            self.p_source.info.setText(src.info_text())
        self.config_btn.setEnabled(bool(src.controls()))
        lo, hi = src.frequency_range()
        self.center_disp.setRange(lo, hi)
        self.center_disp.setEnabled(src.tunable)
        self.freq.setRange(max(0.0, lo - src.sample_rate / 2), hi + src.sample_rate / 2)
        self.spectrum.set_sample_rate(src.sample_rate)
        self.spectrum.set_center(src.center_frequency)
        self.center_disp.setFrequency(src.center_frequency)
        if rebuild_forms and self._dev_dialog is not None:
            self._dev_dialog.close()
            self._dev_dialog = None
        elif self._dev_dialog is not None:
            self._dev_dialog.form.refresh()
        if src.tunable:
            self.set_vfo(self.vfo, snap_to_step=False)
        else:
            self.set_vfo(min(max(self.vfo, src.center_frequency - src.sample_rate / 2),
                             src.center_frequency + src.sample_rate / 2), snap_to_step=False)

    def _on_control_changed(self, key, value):
        if key == "direct_sampling":
            self._source_changed_ui()
        if self._dev_dialog is not None:
            self._dev_dialog.form.refresh()
        if self.p_source.controls is not None:
            self.p_source.controls.refresh()

    def open_device_dialog(self):
        if self.source is None:
            return
        if self._dev_dialog is None:
            self._dev_dialog = DeviceDialog(self.source, self)
            self._dev_dialog.form.changed.connect(self._on_control_changed)
            self._dev_dialog.finished.connect(lambda _: setattr(self, "_dev_dialog", None))
        self._dev_dialog.show()
        self._dev_dialog.raise_()

    def open_file_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open IQ recording", os.path.dirname(self.settings["source"].get("file") or "") or
            self.p_rec.dir.text(), "IQ files (*.wav *.cu8 *.cs8 *.cs16 *.cf32 *.fc32 *.cfile *.raw *.bin *.u8 *.s8);;"
            "All files (*)")
        if not path:
            return
        self.load_file(path)

    def load_file(self, path: str):
        self.stop()
        if self.source is None or self.source.name != "file":
            self.select_source("file")
        src = self.source
        if src.is_open:
            src.close()
        src.path = path
        src.override_center = src.override_rate = src.override_fmt = None
        if self._open_source():
            self.settings["source"]["file"] = path
            self._source_changed_ui()
            self.set_vfo(src.center_frequency + src.sample_rate / 8, snap_to_step=False)

    def _ask_file_metadata(self, e: FileMetadataMissing) -> bool:
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("IQ file parameters")
        f = QtWidgets.QFormLayout(d)
        f.addRow(QtWidgets.QLabel(f"{os.path.basename(e.path)}\nThe file name does not say everything; please enter:"))
        fmt = QtWidgets.QComboBox()
        for k, label in (("cu8", "8-bit unsigned (RTL-SDR, .cu8)"), ("cs8", "8-bit signed (HackRF, .cs8)"),
                         ("cs16", "16-bit signed"), ("cf32", "32-bit float (.cf32 / gqrx)")):
            fmt.addItem(label, k)
        if e.fmt:
            fmt.setCurrentIndex(fmt.findData(e.fmt))
        rate = QtWidgets.QLineEdit(f"{(e.rate or 2.4e6) / 1e6:g}")
        cen = QtWidgets.QLineEdit(f"{(e.center or 100e6) / 1e6:.6f}")
        f.addRow("Sample format", fmt)
        f.addRow("Sample rate (MS/s)", rate)
        f.addRow("Centre frequency (MHz)", cen)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if d.exec_() != QtWidgets.QDialog.Accepted:
            return False
        try:
            r = float(rate.text().replace(",", ".")) * 1e6
            c = parse_frequency(cen.text(), 1e6)
            if r <= 0 or c is None:
                raise ValueError
        except ValueError:
            self._error_box("IQ file", "Invalid sample rate or frequency.")
            return False
        src = self.source
        src.override_fmt = fmt.currentData()
        src.override_rate = r
        src.override_center = c
        return True

    def _seek(self, frac):
        src = self.source
        if isinstance(src, IqFileSource) and src.is_open:
            src.seek(frac * src.duration)

    # ======================================================================= start/stop
    def _set_play_button(self, running: bool):
        self.play.blockSignals(True)
        self.play.setChecked(running)
        self.play.blockSignals(False)
        st = self.style()
        self.play.setIcon(st.standardIcon(QtWidgets.QStyle.SP_MediaStop if running else QtWidgets.QStyle.SP_MediaPlay))
        self.play.setText("Stop" if running else "Start")

    def _on_play_toggled(self, on: bool):
        if on:
            self.start()
        else:
            self.stop()

    def start(self) -> bool:
        src = self.source
        if src is None:
            return False
        if self.engine.running:
            return True
        opened_now = not src.is_open
        if opened_now and not self._open_source():
            self._set_play_button(False)
            return False
        if isinstance(src, IqFileSource) and src.position >= src.duration - 0.01:
            src.seek(0)
        # rebuild the device form only if the device was just opened (gain list etc. now known)
        self._source_changed_ui(rebuild_forms=opened_now or self.p_source.controls is None)
        self._apply_vfo_to_engine()
        try:
            self.engine.start()
        except SourceError as e:
            self._error_box("Cannot start", str(e))
            self._set_play_button(False)
            return False
        except Exception as e:
            log.exception("start failed")
            self._error_box("Cannot start", f"{type(e).__name__}: {e}")
            self._set_play_button(False)
            return False
        self._set_play_button(True)
        self._was_running = True
        return True

    def stop(self):
        if self.engine.running or self.engine._thread is not None:
            self.engine.stop()
        self._set_play_button(False)
        self.p_rec.set_iq_state(False, self.p_rec.iq_info.text())
        self.p_rec.set_af_state(False, self.p_rec.af_info.text())

    def set_sample_rate(self, hz: float):
        src = self.source
        was = self.engine.running
        if was:
            self.engine.stop()
        try:
            src.set_sample_rate(hz)
        except SourceError as e:
            self._error_box("Sample rate", str(e))
        self._source_changed_ui()
        if was:
            self.start()

    # ======================================================================= tuning
    def _tune_ctx(self) -> TuneContext:
        src = self.source
        lo, hi = passband(self.mode, self.engine.params.bandwidth or MODE_INFO[self.mode].bandwidth)
        fmin, fmax = src.frequency_range()
        return TuneContext(src.center_frequency, src.sample_rate, src.usable_bandwidth(), lo, hi, src.tunable,
                           fmin, fmax, self.lo_offset, self.lo_offset_hz)

    def current_step(self) -> float:
        return self.step or MODE_INFO[self.mode].step

    def set_vfo(self, hz: float, snap_to_step: bool = False, follow: bool = True):
        if self.source is None:
            return
        if snap_to_step:
            hz = snap(hz, self.current_step())
        vfo, center = plan(hz, self._tune_ctx())
        if center != self.source.center_frequency:
            try:
                self.source.set_center_frequency(center)
            except SourceError as e:
                self.statusBar().showMessage(str(e), 5000)
                vfo, _ = plan(hz, TuneContext(**{**self._tune_ctx().__dict__, "tunable": False}))
            self.center_disp.setFrequency(self.source.center_frequency)
            self.spectrum.set_center(self.source.center_frequency)
        self.vfo = vfo
        self.freq.setFrequency(vfo)
        self.spectrum.set_vfo(vfo, ensure_visible=follow)
        self._apply_vfo_to_engine()

    def _apply_vfo_to_engine(self):
        self.engine.set_vfo(self.vfo)

    def set_center(self, hz: float):
        src = self.source
        if src is None or not src.tunable:
            return
        try:
            src.set_center_frequency(hz)
        except SourceError as e:
            self._error_box("Tuning", str(e))
        self.center_disp.setFrequency(src.center_frequency)
        self.spectrum.set_center(src.center_frequency)
        # keep the VFO if it is still inside the new span, otherwise bring it along
        ctx = self._tune_ctx()
        half = ctx.usable_bw / 2
        if not (src.center_frequency - half <= self.vfo + ctx.pb_lo and self.vfo + ctx.pb_hi <= src.center_frequency + half):
            self.vfo = src.center_frequency + (self.lo_offset_hz if self.lo_offset else 0)
        self.set_vfo(self.vfo)

    def step_tune(self, n: int):
        st = self.current_step()
        base = snap(self.vfo, st) if self.snap_on else self.vfo
        self.set_vfo(base + n * st, snap_to_step=False)

    def _set_step(self, step: float):
        self.step = step

    def _set_lo_offset(self, on: bool, hz: float):
        self.lo_offset, self.lo_offset_hz = on, hz
        self.set_vfo(self.vfo)

    def tune_bookmark(self, b: Bookmark):
        self.bandwidths[b.mode] = b.bandwidth
        self.set_mode(b.mode, force=True)
        self.set_vfo(b.frequency, snap_to_step=False)

    def add_bookmark(self):
        bw = self.engine.params.bandwidth or MODE_INFO[self.mode].bandwidth
        self.side.sections["bookmarks"].set_expanded(True)
        self.p_bm.add_bookmark(Bookmark(f"{self.vfo / 1e6:.4f} MHz", self.vfo, self.mode, bw))

    # ======================================================================= radio
    def set_mode(self, mode: str, force: bool = False):
        if mode == self.mode and not force:
            return
        if self.engine.params.bandwidth and self.mode in MODES and not force:
            self.bandwidths[self.mode] = self.engine.params.bandwidth
        self.mode = mode
        bw = self.bandwidths.get(mode, MODE_INFO[mode].bandwidth)
        order = self.orders.get(mode, MODE_INFO[mode].order)
        self.engine.set_mode(mode, bw, order)
        self.p_radio.set_mode(mode, bw, order)
        self._update_passband(bw)
        self.set_vfo(self.vfo)

    def set_bandwidth(self, bw: float):
        info = MODE_INFO[self.mode]
        bw = float(min(max(bw, info.min_bw), info.max_bw))
        self.bandwidths[self.mode] = bw
        self.engine.set_bandwidth(bw)
        self._update_passband(bw)
        self.p_radio.set_bandwidth(bw)

    def _drag_bandwidth(self, bw: float):
        step = 10 if bw < 5000 else (100 if bw < 50000 else 1000)
        self.set_bandwidth(round(bw / step) * step)

    def _update_passband(self, bw: float):
        lo, hi = passband(self.mode, bw)
        self.spectrum.set_passband(lo, hi, MODE_INFO[self.mode].sideband)

    def _set_filter(self, window: str, order: int):
        self.orders[self.mode] = int(order)
        self.engine.set_filter(window, int(order))

    # ======================================================================= audio
    def _set_volume(self, v: int):
        self.engine.sink.set_volume(v)

    def _set_mute(self, on: bool):
        self.engine.sink.muted = on
        st = self.style()
        self.mute.setIcon(st.standardIcon(QtWidgets.QStyle.SP_MediaVolumeMuted if on else QtWidgets.QStyle.SP_MediaVolume))

    def _set_audio_device(self, dev):
        lat = self.p_audio.latency.value() / 1000.0
        sink, msg = create_sink(dev, lat, prefer_null=(dev == "null"))
        if msg:
            self._error_box("Audio", msg)
        self.engine.set_sink(sink)
        self.sink = sink
        self._set_volume(self.volume.value())
        self._set_mute(self.mute.isChecked())

    # ======================================================================= display
    def _set_fft(self, d: dict):
        d = dict(d)
        if "frame_rate" in d:
            fps = min(float(d["frame_rate"]), GUI_FPS_CAP)
            d["frame_rate"] = fps * FRAME_OVERSAMPLE
            if hasattr(self, "_gui_timer"):
                self._gui_timer.setInterval(int(1000 / fps))
        self.engine.set_fft(**d)

    def _set_view(self, d: dict):
        sp = self.spectrum
        if "smoothing" in d:
            sp.set_smoothing(d["smoothing"])
        if "peak_hold" in d:
            sp.set_peak_hold(d["peak_hold"])
        if "fill" in d:
            sp.set_fill(d["fill"])
        if "colormap" in d:
            sp.set_colormap(d["colormap"])

    def _levels_changed(self):
        self.spectrum.set_levels(float(self.s_offset.value()), float(self.s_range.value()))

    def auto_levels(self):
        fr = self.engine.last_frame()
        if fr is None:
            return
        floor = noise_floor_db(fr.db)
        cur = self.spectrum.display.current if self.spectrum.display.current is not None else fr.db
        peak = float(cur.max())
        ref = min(30, 5 * math.ceil((peak + 8) / 5))
        rng = max(20, ref - (floor - 12))
        self.s_offset.setValue(ref)
        self.s_range.setValue(rng)
        self._levels_changed()

    def _zoom_slider(self, v: int):
        mz = self.spectrum.max_zoom()
        self.spectrum.set_zoom(mz ** (v / 100.0))

    def _zoom_changed(self, z: float):
        mz = self.spectrum.max_zoom()
        self.s_zoom.setValue(0 if mz <= 1 else 100 * math.log(z) / math.log(mz))

    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    # ======================================================================= recording
    def _rec_dir(self) -> str:
        return self.p_rec.dir.text() or default_recordings_dir()

    def _toggle_iq_rec(self, on: bool):
        if on:
            try:
                rec = self.engine.start_iq_recording(self._rec_dir(), self.p_rec.fmt.currentData())
                self.p_rec.set_iq_state(True, os.path.basename(rec.path))
            except Exception as e:
                self.p_rec.set_iq_state(False)
                self._error_box("Recording", f"Cannot start IQ recording: {e}")
        else:
            self.engine.stop_iq_recording()
            self.p_rec.set_iq_state(False, self.p_rec.iq_info.text())

    def _toggle_af_rec(self, on: bool):
        if on:
            try:
                rec = self.engine.start_audio_recording(self._rec_dir())
                self.p_rec.set_af_state(True, os.path.basename(rec.path))
            except Exception as e:
                self.p_rec.set_af_state(False)
                self._error_box("Recording", f"Cannot start audio recording: {e}")
        else:
            self.engine.stop_audio_recording()
            self.p_rec.set_af_state(False, self.p_rec.af_info.text())

    # ======================================================================= timers
    def _on_gui_tick(self):
        frames = self.engine.take_frames()
        if frames:
            fr = frames[-1]
            if fr.sample_rate != self.spectrum.sample_rate:
                self.spectrum.set_sample_rate(fr.sample_rate)
            self.spectrum.set_frames(frames, draw=True)
            self.frames_shown += 1
        for kind, msg in self.engine.take_events():
            if kind in ("error",):
                self._set_play_button(self.engine.running)
                self._error_box("PyRX", msg)
            elif kind == "eof":
                self.statusBar().showMessage(msg, 8000)
            else:
                self.statusBar().showMessage(msg, 8000)

    def _on_status_tick(self):
        e = self.engine
        st = e.stats()
        if self.play.isChecked() != st.running and e._thread is None:
            self._set_play_button(st.running)
        now, cpu = time.monotonic(), time.process_time()
        dt = now - self._cpu_t[0]
        if dt > 0:
            self.cpu = (cpu - self._cpu_t[1]) / dt * 100
        self._cpu_t = (now, cpu)
        if st.running:
            self.st_level.setText(f"Level {st.level_db:6.1f} dBFS")
            self.st_snr.setText(f"SNR {st.snr_db:5.1f} dB")
            self.level_bar.setValue(int(max(-120, min(0, st.level_db))))
        else:
            self.st_level.setText("Level   –")
            self.st_snr.setText("SNR  –")
            self.level_bar.setValue(-120)
        self.st_rate.setText(f"{st.effective_rate / 1e6:.3f} MS/s ({fmt_rate(st.sample_rate)})")
        drops = st.dropped_blocks + st.source_overflows
        self.st_drop.setText(f"Dropped {drops}")
        self.st_drop.setStyleSheet("color:#ff7070;" if drops else "")
        self.st_audio.setText(f"Audio {st.audio_fill_ms:3.0f} ms  U{st.audio_underruns}/O{st.audio_overruns}")
        self.st_cpu.setText(f"CPU {self.cpu:3.0f}% (DSP {st.dsp_load * 100:3.0f}%)")
        self.st_fps.setText(f"{self.spectrum.fps:4.1f} fps")
        self.p_radio.set_stereo_indicator(st.running and st.stereo)
        self.p_radio.sq_level.setText(f"{st.level_db:.0f} dBFS" if st.running else "")
        self.p_agc.gain_label.setText(f"{st.agc_gain_db:+.1f} dB" if st.running else "")
        sink = e.sink
        self.p_audio.info.setText(
            f"{sink.name} @ {sink.sample_rate:g} Hz, drift corr. {st.drift_ppm:+.0f} ppm\n"
            f"underruns {sink.underruns}, overruns {sink.overruns}")
        src = self.source
        if isinstance(src, IqFileSource) and src.is_open:
            self.p_source.set_position(src.position, src.duration)
        for rec, setter in ((e.iq_rec, self.p_rec.set_iq_state), (e.af_rec, self.p_rec.set_af_state)):
            if rec is None:
                continue
            txt = f"{os.path.basename(rec.path)}\n{fmt_time(rec.elapsed)}  {fmt_size(rec.bytes_written)}"
            if rec.dropped:
                txt += f"  ({rec.dropped} blocks dropped: disk too slow)"
            if rec.error:
                txt += f"\nError: {rec.error}"
            setter(rec.active, txt)

    # ======================================================================= misc
    def _error_box(self, title: str, msg: str):
        """One dialog at a time; while one is open, further messages go to the status bar."""
        self.errors_shown.append(msg)
        if self._dialog_open or QtWidgets.QApplication.platformName() == "offscreen":
            log.warning("%s: %s", title, msg)
            self.statusBar().showMessage(msg, 10000)
            return
        self._dialog_open = True
        try:
            QtWidgets.QMessageBox.warning(self, title, msg)
        finally:
            self._dialog_open = False

    def _show_shortcuts(self):
        txt = "<table>" + "".join(f"<tr><td><b>{k}</b>&nbsp;&nbsp;</td><td>{v}</td></tr>" for k, v in SHORTCUTS) + "</table>"
        QtWidgets.QMessageBox.information(self, "Keyboard shortcuts", txt)

    def _about(self):
        ok, why = sounddevice_available()
        QtWidgets.QMessageBox.about(
            self, "About PyRX",
            "<b>PyRX</b> - a receive-only software-defined radio in Python.<br>"
            "Sources: simulated, IQ files, RTL-SDR, HackRF One.<br>"
            f"Audio: {'sounddevice/PortAudio' if ok else 'unavailable (' + why + ')'}")

    def closeEvent(self, ev):
        if getattr(self, "_closed", False):
            ev.accept()
            return
        self._closed = True
        self.save_settings()
        self._gui_timer.stop()
        self._status_timer.stop()
        self.engine.shutdown()
        for s in self._sources.values():
            if s is not self.source and s.is_open:
                self.engine._call_with_timeout(s.close, 3.0, "closing a device")
        if self._dev_dialog is not None:
            self._dev_dialog.close()
        self.spectrum.shutdown()
        ev.accept()
