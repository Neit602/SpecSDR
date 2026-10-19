"""Side panel sections. Each panel only emits signals; MainWindow wires them to the engine."""

from __future__ import annotations


from PyQt5 import QtCore, QtWidgets

from ..config.bookmarks import Bookmark, BookmarkStore
from ..core.tuning import STEPS
from ..dsp.filters import FILTER_WINDOWS
from ..dsp.receiver import MODE_INFO, MODES
from ..dsp.spectrum import FFT_SIZES, WINDOWS
from .freq_display import parse_frequency
from .theme import COLORMAPS
from .widgets import ControlsForm, ValueSlider, form


def fmt_hz(hz: float) -> str:
    if hz >= 1e6:
        return f"{hz / 1e6:g} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:g} kHz"
    return f"{hz:g} Hz"


def fmt_rate(hz: float) -> str:
    return f"{hz / 1e6:g} MS/s"


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return str(n)


def fmt_time(s: float) -> str:
    s = int(s)
    return f"{s // 3600:d}:{s // 60 % 60:02d}:{s % 60:02d}"


# ============================================================================ Source

class SourcePanel(QtWidgets.QWidget):
    deviceChanged = QtCore.pyqtSignal(str)
    refreshRequested = QtCore.pyqtSignal()
    sampleRateChanged = QtCore.pyqtSignal(float)
    openFileRequested = QtCore.pyqtSignal()
    seekRequested = QtCore.pyqtSignal(float)
    dcChanged = QtCore.pyqtSignal(bool)
    loOffsetChanged = QtCore.pyqtSignal(bool, float)
    swapIqChanged = QtCore.pyqtSignal(bool)
    controlChanged = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        w, f = form()
        lay.addWidget(w)
        row = QtWidgets.QHBoxLayout()
        self.device = QtWidgets.QComboBox()
        self.device.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.refresh = QtWidgets.QToolButton()
        self.refresh.setText("⟳")
        self.refresh.setToolTip("Rescan for devices")
        row.addWidget(self.device, 1)
        row.addWidget(self.refresh)
        f.addRow("Device", row)
        self.file_btn = QtWidgets.QPushButton("Open IQ file…")
        f.addRow(self.file_btn)
        self.position = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.position.setRange(0, 1000)
        self.position.setToolTip("Playback position")
        self.pos_label = QtWidgets.QLabel("")
        prow = QtWidgets.QHBoxLayout()
        prow.addWidget(self.position, 1)
        prow.addWidget(self.pos_label)
        self.pos_row = QtWidgets.QWidget()
        self.pos_row.setLayout(prow)
        prow.setContentsMargins(0, 0, 0, 0)
        f.addRow(self.pos_row)
        self.rate = QtWidgets.QComboBox()
        self.rate.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        f.addRow("Sample rate", self.rate)
        self.dc = QtWidgets.QCheckBox("DC removal")
        self.dc.setToolTip("Remove the DC spike / LO leakage at the centre frequency")
        f.addRow(self.dc)
        orow = QtWidgets.QHBoxLayout()
        self.lo = QtWidgets.QCheckBox("LO offset")
        self.lo.setToolTip("Keep the VFO away from the centre (DC): the hardware is tuned "
                           "this far below the VFO whenever the passband would cover DC")
        self.lo_hz = QtWidgets.QSpinBox()
        self.lo_hz.setRange(10, 5000)
        self.lo_hz.setSuffix(" kHz")
        self.lo_hz.setKeyboardTracking(False)
        orow.addWidget(self.lo)
        orow.addWidget(self.lo_hz)
        f.addRow(orow)
        self.swap = QtWidgets.QCheckBox("Swap I/Q")
        self.swap.setToolTip("Mirror the spectrum (for sources with I and Q exchanged)")
        f.addRow(self.swap)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #8a8f98;")
        f.addRow(self.info)
        self.form_holder = QtWidgets.QVBoxLayout()
        self.form_holder.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(self.form_holder)
        self.controls: ControlsForm | None = None
        self._seeking = False

        self.device.activated.connect(lambda i: self.deviceChanged.emit(str(self.device.itemData(i) or "")))
        self.refresh.clicked.connect(self.refreshRequested)
        self.file_btn.clicked.connect(self.openFileRequested)
        self.rate.activated.connect(lambda i: self.sampleRateChanged.emit(float(self.rate.itemData(i))))
        self.dc.toggled.connect(self.dcChanged)
        self.lo.toggled.connect(lambda on: self.loOffsetChanged.emit(on, self.lo_hz.value() * 1e3))
        self.lo_hz.valueChanged.connect(lambda v: self.loOffsetChanged.emit(self.lo.isChecked(), v * 1e3))
        self.swap.toggled.connect(self.swapIqChanged)
        self.position.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.position.sliderReleased.connect(self._seek)

    def _seek(self):
        self._seeking = False
        self.seekRequested.emit(self.position.value() / 1000.0)

    def set_devices(self, devices, current: str | None, is_file: bool):
        self.device.blockSignals(True)
        self.device.clear()
        for d in devices:
            self.device.addItem(d.label, d.id)
        if not devices:
            self.device.addItem("(no device found)" if not is_file else "(file)", "")
        i = self.device.findData(current) if current else 0
        self.device.setCurrentIndex(max(i, 0))
        self.device.blockSignals(False)
        self.device.setEnabled(bool(devices))
        self.file_btn.setVisible(is_file)
        self.pos_row.setVisible(is_file)
        self.device.setVisible(not is_file)
        self.refresh.setVisible(not is_file)

    def set_rates(self, rates, current: float, fixed: bool):
        self.rate.blockSignals(True)
        self.rate.clear()
        for r in rates:
            self.rate.addItem(fmt_rate(r), float(r))
        i = min(range(self.rate.count()), key=lambda j: abs(self.rate.itemData(j) - current)) if rates else -1
        if i >= 0:
            self.rate.setCurrentIndex(i)
        self.rate.blockSignals(False)
        self.rate.setEnabled(not fixed and len(rates) > 1)

    def set_source(self, source):
        if self.controls is not None:
            self.controls.setParent(None)
            self.controls.deleteLater()
            self.controls = None
        if source is not None and source.controls():
            self.controls = ControlsForm(source, include_groups=False)
            self.controls.changed.connect(self.controlChanged)
            self.form_holder.addWidget(self.controls)

    def set_position(self, pos: float, duration: float):
        if not self._seeking and duration > 0:
            self.position.blockSignals(True)
            self.position.setValue(int(pos / duration * 1000))
            self.position.blockSignals(False)
        self.pos_label.setText(f"{fmt_time(pos)} / {fmt_time(duration)}")


# ============================================================================ Radio

DEEMPH = [(50e-6, "50 µs (Europe, ...)"), (75e-6, "75 µs (Americas, Korea)"), (0.0, "Off")]


class RadioPanel(QtWidgets.QWidget):
    modeChanged = QtCore.pyqtSignal(str)
    bandwidthChanged = QtCore.pyqtSignal(float)
    filterChanged = QtCore.pyqtSignal(str, int)
    stepChanged = QtCore.pyqtSignal(float)
    snapChanged = QtCore.pyqtSignal(bool)
    squelchChanged = QtCore.pyqtSignal(bool, float)
    deemphasisChanged = QtCore.pyqtSignal(float)
    stereoChanged = QtCore.pyqtSignal(bool)
    cwPitchChanged = QtCore.pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        w, f = form()
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(w)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(2)
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_buttons: dict[str, QtWidgets.QToolButton] = {}
        for i, m in enumerate(MODES):
            b = QtWidgets.QToolButton()
            b.setText(m)
            b.setCheckable(True)
            b.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            b.setToolTip(f"{m} (Alt+{i + 1})")
            self.mode_group.addButton(b, i)
            self.mode_buttons[m] = b
            grid.addWidget(b, i // 4, i % 4)
        f.addRow(grid)
        bw_row = QtWidgets.QHBoxLayout()
        self.bandwidth = QtWidgets.QSpinBox()
        self.bandwidth.setRange(10, 400000)
        self.bandwidth.setSingleStep(100)
        self.bandwidth.setSuffix(" Hz")
        self.bandwidth.setKeyboardTracking(False)
        self.presets = QtWidgets.QComboBox()
        self.presets.setToolTip("Bandwidth presets")
        bw_row.addWidget(self.bandwidth, 1)
        bw_row.addWidget(self.presets)
        f.addRow("Bandwidth", bw_row)
        self.window = QtWidgets.QComboBox()
        for k, v in FILTER_WINDOWS.items():
            self.window.addItem(v, k)
        self.order = QtWidgets.QSpinBox()
        self.order.setRange(15, 4095)
        self.order.setSingleStep(32)
        self.order.setKeyboardTracking(False)
        self.order.setToolTip("Channel filter length (taps). Longer = sharper edges, more CPU.")
        f.addRow("Filter", self.window)
        f.addRow("Order", self.order)
        st_row = QtWidgets.QHBoxLayout()
        self.step = QtWidgets.QComboBox()
        self.step.addItem("Auto (per mode)", 0.0)
        for s in STEPS:
            self.step.addItem(fmt_hz(s) if s != 8333.333333 else "8.33 kHz", float(s))
        self.snap = QtWidgets.QCheckBox("Snap")
        self.snap.setToolTip("Snap clicks and drags to the step grid")
        st_row.addWidget(self.step, 1)
        st_row.addWidget(self.snap)
        f.addRow("Step", st_row)
        self.sq = QtWidgets.QCheckBox("Squelch")
        self.sq_level = QtWidgets.QLabel("")
        self.sq_level.setStyleSheet("color: #8a8f98;")
        sq_row = QtWidgets.QHBoxLayout()
        sq_row.addWidget(self.sq)
        sq_row.addStretch(1)
        sq_row.addWidget(self.sq_level)
        f.addRow(sq_row)
        self.sq_thr = ValueSlider(-130, 0, 1, -60, "dBFS")
        self.sq_thr.setToolTip("Squelch threshold (channel power at the VFO)")
        f.addRow("Threshold", self.sq_thr)
        self.deemph = QtWidgets.QComboBox()
        for v, label in DEEMPH:
            self.deemph.addItem(label, v)
        self.deemph_label = QtWidgets.QLabel("De-emphasis")
        f.addRow(self.deemph_label, self.deemph)
        stereo_row = QtWidgets.QHBoxLayout()
        self.stereo = QtWidgets.QCheckBox("FM stereo")
        self.stereo_ind = QtWidgets.QLabel("MONO")
        self.stereo_ind.setAlignment(QtCore.Qt.AlignCenter)
        self.stereo_ind.setMinimumWidth(64)
        stereo_row.addWidget(self.stereo)
        stereo_row.addStretch(1)
        stereo_row.addWidget(self.stereo_ind)
        self.stereo_w = QtWidgets.QWidget()
        self.stereo_w.setLayout(stereo_row)
        stereo_row.setContentsMargins(0, 0, 0, 0)
        f.addRow(self.stereo_w)
        self.cw = QtWidgets.QSpinBox()
        self.cw.setRange(100, 3000)
        self.cw.setSingleStep(10)
        self.cw.setSuffix(" Hz")
        self.cw.setKeyboardTracking(False)
        self.cw_label = QtWidgets.QLabel("CW pitch")
        f.addRow(self.cw_label, self.cw)
        self._mode = "WFM"

        self.mode_group.buttonClicked[int].connect(lambda i: self.modeChanged.emit(MODES[i]))
        self.bandwidth.valueChanged.connect(lambda v: self.bandwidthChanged.emit(float(v)))
        self.presets.activated.connect(self._preset)
        self.window.activated.connect(lambda _: self.filterChanged.emit(self.window.currentData(), self.order.value()))
        self.order.valueChanged.connect(lambda v: self.filterChanged.emit(self.window.currentData(), v))
        self.step.activated.connect(lambda _: self.stepChanged.emit(float(self.step.currentData())))
        self.snap.toggled.connect(self.snapChanged)
        self.sq.toggled.connect(lambda on: self.squelchChanged.emit(on, self.sq_thr.value()))
        self.sq_thr.valueChanged.connect(lambda v: self.squelchChanged.emit(self.sq.isChecked(), v))
        self.deemph.activated.connect(lambda _: self.deemphasisChanged.emit(float(self.deemph.currentData())))
        self.stereo.toggled.connect(self.stereoChanged)
        self.cw.valueChanged.connect(lambda v: self.cwPitchChanged.emit(float(v)))

    def _preset(self, i):
        v = self.presets.itemData(i)
        if v:
            self.bandwidth.setValue(int(v))

    def set_mode(self, mode: str, bandwidth: float, order: int):
        self._mode = mode
        self.mode_buttons[mode].setChecked(True)
        info = MODE_INFO[mode]
        for wdg in (self.bandwidth, self.order, self.presets):
            wdg.blockSignals(True)
        self.bandwidth.setRange(int(info.min_bw), int(info.max_bw))
        self.bandwidth.setSingleStep(1000 if info.max_bw > 50000 else (100 if info.max_bw > 5000 else 10))
        self.bandwidth.setValue(int(round(bandwidth)))
        self.order.setValue(int(order))
        self.presets.clear()
        self.presets.addItem("Presets", None)
        for p in info.presets:
            self.presets.addItem(fmt_hz(p), float(p))
        for wdg in (self.bandwidth, self.order, self.presets):
            wdg.blockSignals(False)
        is_wfm = mode == "WFM"
        for wdg in (self.deemph, self.deemph_label, self.stereo_w):
            wdg.setVisible(is_wfm)
        for wdg in (self.cw, self.cw_label):
            wdg.setVisible(mode == "CW")

    def set_bandwidth(self, bw: float):
        self.bandwidth.blockSignals(True)
        self.bandwidth.setValue(int(round(bw)))
        self.bandwidth.blockSignals(False)

    def set_stereo_indicator(self, on: bool):
        self.stereo_ind.setText("STEREO" if on else "MONO")
        self.stereo_ind.setStyleSheet("background:#1f6f3a;color:white;border-radius:3px;" if on
                                      else "color:#8a8f98;border:1px solid #3a3e45;border-radius:3px;")


# ============================================================================ AGC

class AgcPanel(QtWidgets.QWidget):
    agcChanged = QtCore.pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        w, f = form()
        lay.addWidget(w)
        self.enabled = QtWidgets.QCheckBox("Use AGC")
        f.addRow(self.enabled)
        self.attack = ValueSlider(1, 100, 1, 5, "ms")
        self.decay = ValueSlider(20, 3000, 10, 250, "ms")
        self.hang = ValueSlider(0, 2000, 10, 0, "ms")
        self.threshold = ValueSlider(-130, 0, 1, -90, "dB")
        self.slope = ValueSlider(0, 10, 1, 0, "dB")
        self.manual = ValueSlider(-20, 80, 1, 0, "dB")
        self.attack.setToolTip("How fast the gain drops when the signal gets louder")
        self.decay.setToolTip("How fast the gain recovers when the signal gets quieter")
        self.hang.setToolTip("Hold the gain this long before decaying")
        self.threshold.setToolTip("Signals below this level get no more gain (stops noise being pumped up)")
        self.slope.setToolTip("Output rise in dB for every 10 dB of input above the threshold (0 = flat)")
        self.manual.setToolTip("Fixed audio gain used when AGC is off")
        f.addRow("Attack", self.attack)
        f.addRow("Decay", self.decay)
        f.addRow("Hang", self.hang)
        f.addRow("Threshold", self.threshold)
        f.addRow("Slope", self.slope)
        f.addRow("Gain (AGC off)", self.manual)
        self.gain_label = QtWidgets.QLabel("")
        self.gain_label.setStyleSheet("color: #8a8f98;")
        f.addRow("Current gain", self.gain_label)
        self.enabled.toggled.connect(self._emit)
        for s in (self.attack, self.decay, self.hang, self.threshold, self.slope, self.manual):
            s.valueChanged.connect(self._emit)

    def values(self) -> dict:
        return dict(enabled=self.enabled.isChecked(), attack_ms=self.attack.value(), decay_ms=self.decay.value(),
                    hang_ms=self.hang.value(), threshold_db=self.threshold.value(), slope_db=self.slope.value(),
                    manual_gain_db=self.manual.value())

    def set_values(self, d: dict):
        self.blockSignals(True)
        self.enabled.setChecked(bool(d.get("enabled", True)))
        self.attack.setValue(d.get("attack_ms", 5))
        self.decay.setValue(d.get("decay_ms", 250))
        self.hang.setValue(d.get("hang_ms", 0))
        self.threshold.setValue(d.get("threshold_db", -90))
        self.slope.setValue(d.get("slope_db", 0))
        self.manual.setValue(d.get("manual_gain_db", 0))
        self.blockSignals(False)
        self._sync()

    def _sync(self):
        on = self.enabled.isChecked()
        for s in (self.attack, self.decay, self.hang, self.threshold, self.slope):
            s.setEnabled(on)
        self.manual.setEnabled(not on)

    def _emit(self, *_):
        self._sync()
        self.agcChanged.emit(self.values())


# ============================================================================ Audio

class AudioPanel(QtWidgets.QWidget):
    deviceChanged = QtCore.pyqtSignal(object)
    latencyChanged = QtCore.pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        w, f = form()
        lay.addWidget(w)
        self.device = QtWidgets.QComboBox()
        self.device.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        f.addRow("Output", self.device)
        self.latency = QtWidgets.QSpinBox()
        self.latency.setRange(40, 1000)
        self.latency.setSingleStep(20)
        self.latency.setSuffix(" ms")
        self.latency.setKeyboardTracking(False)
        self.latency.setToolTip("Jitter-buffer target: higher survives CPU hiccups better, lower reacts faster")
        f.addRow("Buffer", self.latency)
        self.info = QtWidgets.QLabel("")
        self.info.setWordWrap(True)
        self.info.setStyleSheet("color: #8a8f98;")
        f.addRow(self.info)
        self.device.activated.connect(lambda i: self.deviceChanged.emit(self.device.itemData(i)))
        self.latency.valueChanged.connect(self.latencyChanged)

    def set_devices(self, devices: list[tuple], current):
        self.device.blockSignals(True)
        self.device.clear()
        self.device.addItem("Default output", "default")
        for idx, name in devices:
            self.device.addItem(name, idx)
        self.device.addItem("None (silent)", "null")
        i = self.device.findData(current)
        self.device.setCurrentIndex(max(i, 0))
        self.device.blockSignals(False)


# ============================================================================ Display

class DisplayPanel(QtWidgets.QWidget):
    fftChanged = QtCore.pyqtSignal(dict)       # engine-side settings
    viewChanged = QtCore.pyqtSignal(dict)      # display-only settings
    autoLevels = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        w, f = form()
        lay.addWidget(w)
        self.size = QtWidgets.QComboBox()
        for n in FFT_SIZES:
            self.size.addItem(str(n), n)
        self.window = QtWidgets.QComboBox()
        for k, v in WINDOWS.items():
            self.window.addItem(v, k)
        self.fps = QtWidgets.QSpinBox()
        self.fps.setRange(5, 60)
        self.fps.setSuffix(" fps")
        self.fps.setKeyboardTracking(False)
        self.averaging = QtWidgets.QSpinBox()
        self.averaging.setRange(1, 16)
        self.averaging.setToolTip("FFTs averaged per displayed frame (50 % overlap)")
        self.averaging.setKeyboardTracking(False)
        self.smoothing = ValueSlider(0, 95, 5, 50, "%")
        self.smoothing.setToolTip("Exponential smoothing of the spectrum trace")
        self.peak = QtWidgets.QCheckBox("Peak hold")
        self.fill = QtWidgets.QCheckBox("Fill spectrum")
        self.speed = ValueSlider(5, 100, 1, 30, "lines/s")
        self.cmap = QtWidgets.QComboBox()
        for k in COLORMAPS:
            self.cmap.addItem(k.capitalize(), k)
        self.auto = QtWidgets.QPushButton("Auto levels")
        self.auto.setToolTip("Fit the spectrum/waterfall level range to the current signal (also: A)")
        f.addRow("FFT size", self.size)
        f.addRow("Window", self.window)
        f.addRow("Frame rate", self.fps)
        f.addRow("Averaging", self.averaging)
        f.addRow("Smoothing", self.smoothing)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.peak)
        row.addWidget(self.fill)
        f.addRow(row)
        f.addRow("Waterfall speed", self.speed)
        f.addRow("Colours", self.cmap)
        f.addRow(self.auto)
        self.size.activated.connect(lambda _: self.fftChanged.emit({"fft_size": self.size.currentData()}))
        self.window.activated.connect(lambda _: self.fftChanged.emit({"window": self.window.currentData()}))
        self.fps.valueChanged.connect(lambda v: self.fftChanged.emit({"frame_rate": float(v)}))
        self.averaging.valueChanged.connect(lambda v: self.fftChanged.emit({"averaging": int(v)}))
        self.speed.valueChanged.connect(lambda v: self.fftChanged.emit({"waterfall_rate": float(v)}))
        self.smoothing.valueChanged.connect(lambda v: self.viewChanged.emit({"smoothing": v / 100.0}))
        self.peak.toggled.connect(lambda on: self.viewChanged.emit({"peak_hold": on}))
        self.fill.toggled.connect(lambda on: self.viewChanged.emit({"fill": on}))
        self.cmap.activated.connect(lambda _: self.viewChanged.emit({"colormap": self.cmap.currentData()}))
        self.auto.clicked.connect(self.autoLevels)

    def set_values(self, d: dict):
        for wdg in (self.size, self.window, self.fps, self.averaging, self.smoothing, self.peak, self.fill,
                    self.speed, self.cmap):
            wdg.blockSignals(True)
        self.size.setCurrentIndex(max(0, self.size.findData(int(d["fft_size"]))))
        self.window.setCurrentIndex(max(0, self.window.findData(d["window"])))
        self.fps.setValue(int(d["fps"]))
        self.averaging.setValue(int(d["averaging"]))
        self.smoothing.setValue(d["smoothing"] * 100)
        self.peak.setChecked(bool(d["peak_hold"]))
        self.fill.setChecked(bool(d["fill"]))
        self.speed.setValue(d["wf_speed"])
        self.cmap.setCurrentIndex(max(0, self.cmap.findData(d["colormap"])))
        for wdg in (self.size, self.window, self.fps, self.averaging, self.smoothing, self.peak, self.fill,
                    self.speed, self.cmap):
            wdg.blockSignals(False)


# ============================================================================ Recording

class RecordingPanel(QtWidgets.QWidget):
    iqToggled = QtCore.pyqtSignal(bool)
    audioToggled = QtCore.pyqtSignal(bool)
    directoryChanged = QtCore.pyqtSignal(str)
    formatChanged = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        w, f = form()
        lay.addWidget(w)
        drow = QtWidgets.QHBoxLayout()
        self.dir = QtWidgets.QLineEdit()
        self.dir.setReadOnly(True)
        self.browse = QtWidgets.QToolButton()
        self.browse.setText("…")
        drow.addWidget(self.dir, 1)
        drow.addWidget(self.browse)
        f.addRow("Folder", drow)
        self.fmt = QtWidgets.QComboBox()
        self.fmt.addItem("16-bit integer", "cs16")
        self.fmt.addItem("8-bit unsigned (RTL native)", "cu8")
        self.fmt.addItem("32-bit float", "cf32")
        f.addRow("IQ format", self.fmt)
        self.iq_btn = QtWidgets.QPushButton("● Record baseband IQ")
        self.iq_btn.setCheckable(True)
        self.iq_info = QtWidgets.QLabel("")
        self.af_btn = QtWidgets.QPushButton("● Record audio")
        self.af_btn.setCheckable(True)
        self.af_info = QtWidgets.QLabel("")
        for lab in (self.iq_info, self.af_info):
            lab.setStyleSheet("color: #8a8f98;")
            lab.setWordWrap(True)
        f.addRow(self.iq_btn)
        f.addRow(self.iq_info)
        f.addRow(self.af_btn)
        f.addRow(self.af_info)
        self.browse.clicked.connect(self._browse)
        self.iq_btn.toggled.connect(self.iqToggled)
        self.af_btn.toggled.connect(self.audioToggled)
        self.fmt.activated.connect(lambda _: self.formatChanged.emit(self.fmt.currentData()))

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Recording folder", self.dir.text())
        if d:
            self.dir.setText(d)
            self.directoryChanged.emit(d)

    @staticmethod
    def _set_checked(btn, on):
        btn.blockSignals(True)
        btn.setChecked(on)
        btn.blockSignals(False)
        btn.setStyleSheet("background:#8a1f1f;color:white;" if on else "")

    def set_iq_state(self, on: bool, text: str = ""):
        self._set_checked(self.iq_btn, on)
        self.iq_btn.setText("■ Stop IQ recording" if on else "● Record baseband IQ")
        if text or not on:
            self.iq_info.setText(text)

    def set_af_state(self, on: bool, text: str = ""):
        self._set_checked(self.af_btn, on)
        self.af_btn.setText("■ Stop audio recording" if on else "● Record audio")
        if text or not on:
            self.af_info.setText(text)


# ============================================================================ Frequency manager

class BookmarkDialog(QtWidgets.QDialog):
    def __init__(self, bm: Bookmark, groups: list[str], parent=None, title="Bookmark"):
        super().__init__(parent)
        self.setWindowTitle(title)
        f = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit(bm.name)
        self.freq = QtWidgets.QLineEdit(f"{bm.frequency / 1e6:.6f}M")
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(MODES)
        self.mode.setCurrentText(bm.mode)
        self.bw = QtWidgets.QSpinBox()
        self.bw.setRange(10, 400000)
        self.bw.setSuffix(" Hz")
        self.bw.setValue(int(bm.bandwidth))
        self.group = QtWidgets.QComboBox()
        self.group.setEditable(True)
        self.group.addItems(sorted(set(groups) | {"General"}))
        self.group.setCurrentText(bm.group)
        f.addRow("Name", self.name)
        f.addRow("Frequency", self.freq)
        f.addRow("Mode", self.mode)
        f.addRow("Bandwidth", self.bw)
        f.addRow("Group", self.group)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        f.addRow(bb)
        self.result_bm = None
        self._bm = bm

    def _ok(self):
        hz = parse_frequency(self.freq.text())
        if hz is None or not self.name.text().strip():
            QtWidgets.QMessageBox.warning(self, "Bookmark", "Please enter a name and a valid frequency.")
            return
        self.result_bm = Bookmark(self.name.text().strip(), hz, self.mode.currentText(), float(self.bw.value()),
                                  self.group.currentText().strip() or "General", self._bm.id)
        self.accept()


class BookmarksPanel(QtWidgets.QWidget):
    tuneRequested = QtCore.pyqtSignal(object)   # Bookmark
    addRequested = QtCore.pyqtSignal()

    def __init__(self, store: BookmarkStore, parent=None):
        super().__init__(parent)
        self.store = store
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 6)
        top = QtWidgets.QHBoxLayout()
        self.group = QtWidgets.QComboBox()
        top.addWidget(QtWidgets.QLabel("Group"))
        top.addWidget(self.group, 1)
        lay.addLayout(top)
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "MHz", "Mode", "BW"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.setMinimumHeight(140)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        for c in (1, 2, 3):
            hdr.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeToContents)
        lay.addWidget(self.tree)
        row = QtWidgets.QHBoxLayout()
        self.add = QtWidgets.QPushButton("Add current")
        self.add.setToolTip("Bookmark the current VFO frequency, mode and bandwidth (Ctrl+B)")
        self.edit = QtWidgets.QPushButton("Edit")
        self.delete = QtWidgets.QPushButton("Delete")
        for b in (self.add, self.edit, self.delete):
            row.addWidget(b)
        lay.addLayout(row)
        self.add.clicked.connect(self.addRequested)
        self.edit.clicked.connect(self._edit)
        self.delete.clicked.connect(self._delete)
        self.tree.itemDoubleClicked.connect(self._tune)
        self.group.activated.connect(lambda _: self.reload())
        self.reload()

    def reload(self):
        cur = self.group.currentData()
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItem("All", None)
        for g in self.store.groups():
            self.group.addItem(g, g)
        i = self.group.findData(cur)
        self.group.setCurrentIndex(max(i, 0))
        self.group.blockSignals(False)
        sel = self.group.currentData()
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        for b in self.store.items:
            if sel and b.group != sel:
                continue
            it = QtWidgets.QTreeWidgetItem([b.name, f"{b.frequency / 1e6:.6f}", b.mode, fmt_hz(b.bandwidth)])
            it.setData(0, QtCore.Qt.UserRole, b.id)
            it.setToolTip(0, f"{b.name}\nGroup: {b.group}")
            self.tree.addTopLevelItem(it)
        self.tree.setSortingEnabled(True)

    def _selected(self) -> Bookmark | None:
        it = self.tree.currentItem()
        return self.store.get(it.data(0, QtCore.Qt.UserRole)) if it else None

    def _tune(self, it, _col=0):
        b = self.store.get(it.data(0, QtCore.Qt.UserRole))
        if b:
            self.tuneRequested.emit(b)

    def add_bookmark(self, bm: Bookmark):
        d = BookmarkDialog(bm, self.store.groups(), self, "Add bookmark")
        if d.exec_() == QtWidgets.QDialog.Accepted and d.result_bm:
            self.store.add(d.result_bm)
            self._save()

    def _edit(self):
        b = self._selected()
        if not b:
            return
        d = BookmarkDialog(b, self.store.groups(), self, "Edit bookmark")
        if d.exec_() == QtWidgets.QDialog.Accepted and d.result_bm:
            idx = self.store.items.index(b)
            self.store.items[idx] = d.result_bm
            self._save()

    def _delete(self):
        b = self._selected()
        if b and QtWidgets.QMessageBox.question(self, "Delete bookmark", f"Delete '{b.name}'?") == \
                QtWidgets.QMessageBox.Yes:
            self.store.remove(b.id)
            self._save()

    def _save(self):
        try:
            self.store.save()
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "Bookmarks", f"Could not save bookmarks: {e}")
        self.reload()
