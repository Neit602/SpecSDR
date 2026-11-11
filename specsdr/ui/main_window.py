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
from ..config.fpv_channels import FPV_BW, group_channels, make_bookmark, nearest_channel, standard_channels
from ..config.settings import Settings, default_recordings_dir
from ..core.engine import Engine
from ..core.fpv import FpvScanner
from ..core.recorder import VideoRecorder, snapshot_filename, video_filename
from ..core.tuning import TuneContext, plan, snap
from ..dsp.receiver import MODE_INFO, MODES, passband
from ..dsp.spectrum import noise_floor_db
from ..dsp.fpv.decoder import FpvParams
from ..sources import SourceError, source_classes
from ..sources.iqfile import FileMetadataMissing, IqFileSource
from .freq_display import FrequencyDisplay, parse_frequency
from .fpv import FpvBridge, FpvDock, FpvPanel, FpvScannerPanel
from .panels import (AgcPanel, AudioPanel, BookmarksPanel, DisplayPanel, RadioPanel, RecordingPanel,
                     SourcePanel, fmt_rate, fmt_size, fmt_time)
from .plots import SpectrumPanel
from .widgets import DeviceDialog, SidePanel

log = logging.getLogger(__name__)

FpvScanner_STEP = 8e6      # range scan step: the video carrier is always within 4 MHz of a tune point
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
    ("Alt+1 … Alt+9", "Mode: " + ", ".join(MODES)),
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
        self.setWindowTitle("SpecSDR")
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
        self._fpv_saved: dict | None = None
        self._fpv_active = False
        self._fpv_bw = 18e6
        self._fpv_busy = False
        self._video_rec: VideoRecorder | None = None
        self._fpv_last = None           # last frame painted
        self._scanner: FpvScanner | None = None
        self._scan_view_center: float | None = None

        no_audio = bool(args and getattr(args, "no_audio", False)) or os.environ.get("SPECSDR_NO_AUDIO") == "1"
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
        self.p_fpv = FpvPanel(self.bookmarks)
        self.p_scan = FpvScannerPanel(self.bookmarks)
        self.side.add_section("fpv", "FPV Video", self.p_fpv, False)
        self.side.add_section("fpv_scan", "FPV Scanner", self.p_scan, False)
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

        # ---- FPV video panel
        self.fpv_dock = FpvDock(self)
        self.fpv_dock.setMinimumSize(320, 240)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.fpv_dock)
        self.resizeDocks([self.fpv_dock], [620], QtCore.Qt.Horizontal)
        self.fpv_dock.hide()
        self.fpv_bridge = FpvBridge(self)
        self.fpv_bridge.frameReady.connect(self._fpv_show_frame)
        self.fpv_bridge.scanProgress.connect(self._scan_progress)
        self.fpv_bridge.scanFinished.connect(self._scan_finished)

        # ---- status bar
        sb = self.statusBar()
        self.st_level = QtWidgets.QLabel()
        self.st_snr = QtWidgets.QLabel()
        self.st_rate = QtWidgets.QLabel()
        self.st_drop = QtWidgets.QLabel()
        self.st_audio = QtWidgets.QLabel()
        self.st_cpu = QtWidgets.QLabel()
        self.st_fps = QtWidgets.QLabel()
        self.st_fpv = QtWidgets.QLabel()
        self.st_level.setToolTip("Channel power inside the VFO passband")
        self.st_snr.setToolTip("Channel power relative to the noise floor estimated from the spectrum")
        self.st_rate.setToolTip("Sample rate actually arriving from the source (nominal in brackets)")
        self.st_drop.setToolTip("Blocks lost: engine queue full + reported by the source/driver")
        self.st_audio.setToolTip("Audio buffer fill, underruns / overruns, clock-drift correction")
        self.st_cpu.setToolTip("CPU used by SpecSDR in % of one core (DSP thread load in brackets)")
        self.st_fpv.setToolTip("FPV video decoder status")
        for w in (self.st_fpv, self.st_level, self.st_snr, self.st_rate, self.st_drop, self.st_audio, self.st_cpu,
                  self.st_fps):
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
        m.addAction("FPV video panel", lambda: self.fpv_dock.show())
        m = mb.addMenu("&Help")
        m.addAction("Keyboard shortcuts", self._show_shortcuts)
        m.addAction("About SpecSDR", self._about)

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
        self.p_bm.changed.connect(self._fpv_channels_changed)
        pf = self.p_fpv
        pf.enableRequested.connect(self._fpv_toggle_requested)
        pf.channelSelected.connect(self.fpv_tune)
        pf.paramsChanged.connect(self._fpv_params)
        pf.viewChanged.connect(self._fpv_view)
        pf.snapshotRequested.connect(self.fpv_snapshot)
        pf.recordToggled.connect(self._fpv_record)
        pf.showVideoRequested.connect(self.fpv_dock.show)
        pf.fullscreenRequested.connect(self._fpv_fullscreen)
        pf.addChannelRequested.connect(self._fpv_add_channel)
        pf.audioChanged.connect(self._fpv_audio)
        ps2 = self.p_scan
        ps2.scanRequested.connect(self._scan_start)
        ps2.stopRequested.connect(self._scan_stop)
        ps2.tuneRequested.connect(self.fpv_tune)

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

        fpv = s["fpv"]
        self.p_fpv.apply_settings(fpv)
        self.p_scan.apply_settings(fpv.get("scan", {}))
        self._fpv_view({"aspect": 4.0 / 3.0 if int(fpv.get("aspect", 0)) == 0 else 16.0 / 9.0,
                        "overlay": bool(fpv.get("overlay", True)),
                        "no_signal_screen": bool(fpv.get("no_signal_screen", True))})
        self._fpv_dock_visible = bool(fpv.get("dock_visible", True))

        # source + tuning (command line wins)
        args = self.args
        name = (args.source if args and args.source else None) or src.get("name", "simulated")
        if name not in [self.source_combo.itemData(i) for i in range(self.source_combo.count())]:
            name = "simulated"
        mode = radio.get("mode", "WFM") if radio.get("mode") in MODES else "WFM"
        if args and getattr(args, "mode", None):
            mode = args.mode.upper()
        if mode not in MODES:
            log.warning("unknown mode %s; using WFM", mode)
            mode = "WFM"
        self.mode = mode
        self.select_source(name, initial=True)
        vfo = float(radio.get("vfo", 99.8e6))
        if args and getattr(args, "freq", None):
            vfo = float(args.freq)
        if mode == "FPV":
            self._initial_fpv_freq = vfo if args and getattr(args, "freq", None) else None
        self.set_mode(mode, force=True)
        if mode == "FPV":
            pass                        # _enter_fpv tuned to the channel
        elif self.source is not None and self.source.tunable:
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
        radio["vfo"] = self.vfo if self.mode != "FPV" else (self._fpv_saved or {}).get("vfo", self.vfo)
        radio["mode"] = self.mode
        if self.mode != "FPV":
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
        fp = self.p_fpv.settings()
        fp["scan"] = self.p_scan.settings()
        fp["dock_visible"] = self.fpv_dock.isVisible()
        fp["last_freq"] = self.vfo if self.mode == "FPV" else s["fpv"].get("last_freq", 5800e6)
        s.data["fpv"] = fp
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
        self._fpv_update_availability()
        if self._fpv_active:
            if self._fpv_source_ok()[0]:
                self._fpv_reconfigure()
            else:
                self.set_mode(self._fpv_saved.get("mode", "WFM") if self._fpv_saved else "WFM", force=True)
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
        self._fpv_update_availability()
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
        if self._scanner is not None:
            self._scanner.stop()
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
        if self._fpv_active:
            self._fpv_reconfigure()
        if was:
            self.start()

    # ======================================================================= tuning
    def _tune_ctx(self) -> TuneContext:
        src = self.source
        lo, hi = passband(self.mode, self.engine.params.bandwidth or MODE_INFO[self.mode].bandwidth)
        if self.mode == "FPV":
            lo, hi = passband("FPV", FPV_BW)
        fmin, fmax = src.frequency_range()
        lo_on = self.lo_offset and self.mode != "FPV"   # video is received at DC
        return TuneContext(src.center_frequency, src.sample_rate, src.usable_bandwidth(), lo, hi, src.tunable,
                           fmin, fmax, lo_on, self.lo_offset_hz)

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
        bw = FPV_BW if self.mode == "FPV" else self.engine.params.bandwidth or MODE_INFO[self.mode].bandwidth
        self.side.sections["bookmarks"].set_expanded(True)
        self.p_bm.add_bookmark(Bookmark(f"{self.vfo / 1e6:.4f} MHz", self.vfo, self.mode, bw))

    # ======================================================================= radio
    def set_mode(self, mode: str, force: bool = False):
        if mode == self.mode and not force:
            return
        if mode == "FPV":
            self._enter_fpv()
            return
        if self.mode == "FPV":
            self._leave_fpv(new_mode=mode)
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
        if self.mode == "FPV":              # the video bandwidth is set in the FPV Video section
            self.p_radio.set_bandwidth(FPV_BW)
            return
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
                self._error_box("SpecSDR", msg)
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
        self._fpv_status_tick(st)
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

    # ======================================================================= FPV video
    FPV_RATE = 20e6
    FPV_BB_FILTER = 20e6                # HackRF baseband filter: passes the full FM spectrum at 20 MS/s

    def _fpv_source_ok(self, src=None) -> tuple[bool, str]:
        src = src or self.source
        if src is None:
            return False, "No source"
        if src.name == "rtlsdr":
            return False, ("FPV video needs a HackRF: RTL-SDR tuners reach at most 3.2 MS/s of bandwidth and about "
                           "1.7 GHz, too small for FM video (15-20 MHz wide, mostly at 5.8 GHz).")
        return True, ""

    def _fpv_update_availability(self) -> None:
        ok, why = self._fpv_source_ok()
        self.p_radio.mode_buttons["FPV"].setEnabled(ok)
        self.p_radio.mode_buttons["FPV"].setToolTip(why or f"FPV video (Alt+{MODES.index('FPV') + 1})")
        self.p_fpv.set_available(ok, why)
        src = self.source
        tunable = src is not None and src.tunable
        self.p_scan.set_available(tunable, "" if tunable else "The scanner needs a tunable source (not an IQ file)")

    def _enter_fpv(self) -> None:
        src = self.source
        ok, why = self._fpv_source_ok()
        if not ok:
            self.p_radio.set_mode(self.mode if self.mode != "FPV" else "WFM", self.bandwidths.get("WFM", 200000.0), 127)
            self.p_fpv.set_active(False)
            self._error_box("FPV Video", why)
            return
        if not self._fpv_active:
            self._fpv_saved = dict(mode=self.mode if self.mode != "FPV" else "WFM", vfo=self.vfo,
                                   center=src.center_frequency, rate=src.sample_rate,
                                   bb=src.get_control("bb_filter"), bandwidth=self.engine.params.bandwidth)
        self.mode = "FPV"
        info = MODE_INFO["FPV"]
        self.p_radio.set_mode("FPV", FPV_BW, info.order)
        self._update_passband(FPV_BW)
        if src.tunable:
            if src.sample_rate != self.FPV_RATE:
                rng = src.sample_rate_range()
                if (rng and rng[0] <= self.FPV_RATE <= rng[1]) or self.FPV_RATE in src.sample_rates():
                    self.set_sample_rate(self.FPV_RATE)
            if src.get_control("bb_filter") is not None:
                src.set_control("bb_filter", self.FPV_BB_FILTER)
                self._on_control_changed("bb_filter", self.FPV_BB_FILTER)
        self._fpv_active = True
        freq = getattr(self, "_initial_fpv_freq", None)
        self._initial_fpv_freq = None
        if freq is None and src.tunable:
            freq = self.p_fpv.manual_value() or self.p_fpv.current_channel()
        if freq is None:
            freq = src.center_frequency
        self._fpv_reconfigure()
        self.set_vfo(freq, snap_to_step=False)
        self.spectrum.set_passband(*passband("FPV", FPV_BW), MODE_INFO["FPV"].sideband)
        self.p_fpv.select_frequency(self.vfo)
        self.side.sections["fpv"].set_expanded(True)
        if self._fpv_dock_visible:
            self.fpv_dock.show()
        self.p_fpv.set_active(True, self._fpv_rate_note())
        self.p_scan.set_active(True)

    def _leave_fpv(self, new_mode: str | None = None) -> None:
        if self._scanner is not None:
            self._scanner.stop()
        if self._video_rec is not None:
            self._fpv_record(False)
        self.engine.fpv_disable()
        self._fpv_active = False
        saved = self._fpv_saved or {}
        src = self.source
        self.p_fpv.set_active(False)
        self.p_scan.set_active(False)
        self.fpv_dock.hide()
        mode = new_mode or saved.get("mode", "WFM")
        if src is not None and src.tunable:
            rate = saved.get("rate")
            if rate and rate != src.sample_rate:
                self.mode = mode
                self.set_sample_rate(rate)
            if saved.get("bb") is not None or "bb_filter" in src.control_state():
                src.set_control("bb_filter", saved.get("bb"))
                self._on_control_changed("bb_filter", saved.get("bb"))
        self.mode = mode
        bw = self.bandwidths.get(mode, MODE_INFO[mode].bandwidth)
        order = self.orders.get(mode, MODE_INFO[mode].order)
        self.engine.set_mode(mode, bw, order)
        self.p_radio.set_mode(mode, bw, order)
        self._update_passband(bw)
        self.set_vfo(saved.get("vfo", self.vfo))
        if src is not None and src.tunable and "center" in saved:
            self.set_center(saved["center"])
        self._fpv_saved = None

    def _fpv_rate_note(self) -> str:
        rate = self.source.sample_rate
        if rate < 16e6:
            return f"Warning: {rate / 1e6:g} MS/s is below 16 MS/s; the picture will be degraded."
        return "Receiving"

    def _fpv_reconfigure(self) -> None:
        """(Re)create the decoder for the current sample rate."""
        src = self.source
        params = FpvParams(**self.p_fpv.decoder_params())
        params.show_raw = not self.p_fpv.settings()["no_signal_screen"]
        worker = self.engine.fpv_enable(params, self._fpv_on_frame)
        worker.audio.enabled = self.p_fpv.audio_on.isChecked()
        worker.audio.set_subcarrier(float(self.p_fpv.subcarrier.currentData()))
        self.p_fpv.set_active(True, self._fpv_rate_note())
        if src.sample_rate < 16e6:
            self.statusBar().showMessage(self._fpv_rate_note(), 10000)

    def _fpv_toggle_requested(self, on: bool) -> None:
        if on:
            self.set_mode("FPV")
        elif self.mode == "FPV":
            self.set_mode((self._fpv_saved or {}).get("mode", "WFM"))

    def fpv_tune(self, hz: float) -> None:
        """Tune to a channel: enters FPV mode if needed and centres the receiver on it."""
        if self.mode != "FPV":
            self.set_mode("FPV")
            if self.mode != "FPV":
                return
        self.set_vfo(hz, snap_to_step=False)
        self.p_fpv.select_frequency(self.vfo)

    def _fpv_params(self, d: dict) -> None:
        worker = self.engine.fpv
        if worker is not None:
            worker.post(lambda: worker.decoder.set_params(**d))

    def _fpv_view(self, d: dict) -> None:
        view = self.fpv_dock.view
        if "aspect" in d:
            view.set_aspect(d["aspect"])
        if "overlay" in d:
            view.overlay = d["overlay"]
            view.update()
        if "no_signal_screen" in d:
            self._fpv_no_signal_screen = d["no_signal_screen"]
            worker = self.engine.fpv
            if worker is not None:
                worker.post(lambda v=not d["no_signal_screen"]: worker.decoder.set_params(show_raw=v))

    def _fpv_audio(self, d: dict) -> None:
        worker = self.engine.fpv
        if worker is None:
            return
        if "enabled" in d:
            worker.audio.enabled = d["enabled"]
        if "subcarrier" in d:
            worker.post(lambda v=d["subcarrier"]: worker.audio.set_subcarrier(v))

    def _fpv_channels_changed(self) -> None:
        self.p_fpv.reload_channels()
        self.p_scan.reload_groups()

    def _fpv_add_channel(self) -> None:
        hz = self.p_fpv.manual_value() or self.vfo
        name = f"{hz / 1e6:.0f} MHz"
        self.p_bm.add_bookmark(make_bookmark(name, hz, "FPV"))
        self._fpv_channels_changed()

    def _fpv_fullscreen(self) -> None:
        self.fpv_dock.show()
        self.fpv_dock.toggle_fullscreen()

    def _fpv_on_frame(self, frame) -> None:
        """Decoder thread. Never blocks: display is latest-wins."""
        self._fpv_last = frame
        rec = self._video_rec
        if rec is not None:
            rec.push(frame.rgb)
        if not self._fpv_busy:
            self._fpv_busy = True
            self.fpv_bridge.frameReady.emit()

    def _fpv_show_frame(self) -> None:
        self._fpv_busy = False
        frame = self._fpv_last
        if frame is not None:
            self.fpv_dock.view.set_frame(frame.rgb)

    def fpv_snapshot(self) -> str | None:
        frame = self._fpv_last
        if frame is None:
            self.statusBar().showMessage("No decoded frame to save yet", 4000)
            return None
        path = snapshot_filename(self._rec_dir(), self.vfo)
        self._save_frame_png(frame, path)
        self.statusBar().showMessage(f"Saved {os.path.basename(path)}", 6000)
        return path

    def _save_frame_png(self, frame, path: str) -> bool:
        try:
            d = os.path.dirname(os.path.abspath(path))
            os.makedirs(d, exist_ok=True)
            rgb = frame.rgb
            h, w = rgb.shape[:2]
            img = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888)
            return bool(img.save(path, "PNG"))
        except OSError as e:
            log.error("snapshot failed: %s", e)
            return False

    def _fpv_record(self, on: bool) -> None:
        pf = self.p_fpv
        if on:
            frame = self._fpv_last
            if frame is None:
                self.statusBar().showMessage("No decoded video yet", 4000)
                pf.record_btn.blockSignals(True)
                pf.record_btn.setChecked(False)
                pf.record_btn.blockSignals(False)
                return
            h, w = frame.rgb.shape[:2]
            fps = 25.0 if frame.standard == "PAL" else 29.97
            path = video_filename(self._rec_dir(), self.vfo)
            try:
                rec = VideoRecorder(path, w, h, fps)
                rec.start()
            except Exception as e:
                self._error_box("Recording", f"Cannot start video recording: {e}")
                pf.record_btn.blockSignals(True)
                pf.record_btn.setChecked(False)
                pf.record_btn.blockSignals(False)
                return
            self._video_rec = rec
            pf.record_btn.setText("■ Stop recording")
            pf.record_btn.setStyleSheet("background:#8a1f1f;color:white;")
            pf.record_info.setText(os.path.basename(path))
        else:
            rec, self._video_rec = self._video_rec, None
            if rec is not None:
                rec.stop()
                pf.record_info.setText(f"{os.path.basename(rec.path)}: {rec.frames_written} frames"
                                       + (f", {rec.dropped} dropped" if rec.dropped else "")
                                       + (f"\nError: {rec.error}" if rec.error else ""))
            pf.record_btn.blockSignals(True)
            pf.record_btn.setChecked(False)
            pf.record_btn.blockSignals(False)
            pf.record_btn.setText("● Record video")
            pf.record_btn.setStyleSheet("")

    def _fpv_status_tick(self, st) -> None:
        worker = self.engine.fpv
        view = self.fpv_dock.view
        if worker is None:
            self.st_fpv.setText("")
            return
        s = worker.status()
        locked = s.h_locked and s.v_locked
        view.show_no_signal = (not locked) and bool(getattr(self, "_fpv_no_signal_screen", True))
        if view.overlay:
            drops = s.dropped_samples + st.dropped_blocks * 131072 + (st.source_overflows or 0) * 131072
            view.overlay_lines = [
                f"{self.vfo / 1e6:.3f} MHz   {s.standard}  {'inv' if s.polarity < 0 else ''}",
                f"H {'LOCK' if s.h_locked else '----'}   V {'LOCK' if s.v_locked else '----'}   "
                f"Colour {'LOCK' if s.colour_locked else 'off'}",
                f"Carrier offset {s.carrier_offset / 1e3:+.0f} kHz",
                f"Level {s.level_db:.1f} dBFS   CNR {s.cnr_db:.1f} dB",
                f"{s.fps:.1f} fps   latency {s.latency_ms:.0f} ms",
                f"Dropped: {s.dropped_fields} fields, {drops} samples",
            ]
            view.update()
        self.st_fpv.setText(f"FPV {s.standard} {'LOCK' if locked else 'unlocked'} {s.fps:.1f} fps")

    # -----------------------------------------------------------------------
    def _scan_start(self, d: dict) -> None:
        if self.mode != "FPV":
            self.p_scan.set_running(False)
            return
        if not self.engine.running and not self.start():
            self.p_scan.set_running(False)
            return
        if d["mode"] == "channels":
            names = []
            picked = [b.partition(":") for b in d["bands"]]
            std = {name for kind, _, name in picked if kind == "std"}
            if std:
                names = [hz for _, hz in standard_channels(std)]
            for kind, _, group in picked:
                if kind == "user":
                    names += [hz for _, hz in group_channels(self.bookmarks, group)]
            freqs = sorted(set(names))
        else:
            lo, hi = d["f_lo"], d["f_hi"]
            if hi <= lo:
                self.p_scan.state_label.setText("Enter a valid range")
                self.p_scan.set_running(False)
                return
            step = FpvScanner_STEP
            freqs = [lo + step / 2 + i * step for i in range(int((hi - lo) / step) + 1) if lo + step / 2 + i * step <= hi]
        if not freqs:
            self.p_scan.state_label.setText("Nothing to scan")
            self.p_scan.set_running(False)
            return
        sc = FpvScanner(self.engine, lambda f: nearest_channel(f, self.bookmarks))
        sc.on_update = lambda: self.fpv_bridge.scanProgress.emit()
        sc.on_finished = lambda: self.fpv_bridge.scanFinished.emit()
        self._scanner = sc
        self._scan_view_center = self.source.center_frequency
        self.p_scan.set_running(True)
        self.p_scan.state_label.setText(f"Scanning 0/{len(freqs)} ...")
        sc.start(freqs, d["continuous"])

    def _scan_stop(self) -> None:
        if self._scanner is not None:
            self._scanner.stop()

    def _scan_progress(self) -> None:
        sc = self._scanner
        if sc is None:
            return
        self.p_scan.set_results(sc.results.values())
        self.p_scan.state_label.setText(f"Scanning {sc.step_done}/{sc.steps_total}  found {len(sc.results)}")

    def _scan_finished(self) -> None:
        sc = self._scanner
        if sc is None:
            return
        self._scanner = None
        self.p_scan.set_results(sc.results.values())
        self.p_scan.set_running(False)
        msg = f"{sc.step_done} steps in {sc.last_duration:.1f} s, {len(sc.results)} video signal(s)"
        if sc.error:
            msg = f"Scan failed: {sc.error}"
        self.p_scan.state_label.setText(msg)
        best = sc.strongest()
        if best is not None and self.p_scan.autotune.isChecked() and not sc.error:
            self.fpv_tune(best.frequency)
        else:
            self.set_vfo(self.vfo, snap_to_step=False)
            self.set_center(self._scan_view_center or self.source.center_frequency)

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
            self, "About SpecSDR",
            "<b>SpecSDR</b> - a receive-only software-defined radio in Python.<br>"
            "Sources: simulated, IQ files, RTL-SDR, HackRF One.<br>"
            f"Audio: {'sounddevice/PortAudio' if ok else 'unavailable (' + why + ')'}")

    def closeEvent(self, ev):
        if getattr(self, "_closed", False):
            ev.accept()
            return
        self._closed = True
        if self._scanner is not None:
            self._scanner.stop()
        snap = getattr(self.args, "snapshot", None) if self.args else None
        if snap and self._fpv_last is not None:
            self._save_frame_png(self._fpv_last, snap)
        if self._video_rec is not None:
            self._fpv_record(False)
        self.save_settings()
        self._gui_timer.stop()
        self._status_timer.stop()
        self.engine.fpv_disable()
        self.engine.shutdown()
        for s in self._sources.values():
            if s is not self.source and s.is_open:
                self.engine._call_with_timeout(s.close, 3.0, "closing a device")
        if self._dev_dialog is not None:
            self._dev_dialog.close()
        self.spectrum.shutdown()
        if self.fpv_dock.is_fullscreen():
            self.fpv_dock._end_fullscreen()
        ev.accept()
