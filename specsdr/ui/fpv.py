"""FPV video UI: the video view (dockable, full screen), the FPV Video and FPV Scanner side-panel
sections, and the Qt bridge that carries frames from the decoder thread to the GUI."""

from __future__ import annotations

import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from ..config.bookmarks import BookmarkStore
from ..config.fpv_channels import BANDS, channel_name, group_channels, user_groups
from ..dsp.fpv.audio import SUBCARRIERS
from .freq_display import parse_frequency
from .theme import TEXT, TEXT_DIM
from .widgets import ValueSlider, form


class FpvBridge(QtCore.QObject):
    """Decoder/scanner threads emit these; Qt delivers them on the GUI thread (queued)."""

    frameReady = QtCore.pyqtSignal()      # no payload: queued Python objects can outlive the interpreter
    scanProgress = QtCore.pyqtSignal()
    scanFinished = QtCore.pyqtSignal()


class VideoView(QtWidgets.QWidget):
    """Video surface: keeps the aspect ratio, draws the status overlay and the NO SIGNAL screen."""

    fullscreenToggled = QtCore.pyqtSignal(bool)
    doubleClicked = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame: tuple | None = None
        self.aspect = 4.0 / 3.0
        self.overlay = True
        self.overlay_lines: list[str] = []
        self.show_no_signal = False
        self.no_signal_text = "NO SIGNAL"
        self.setMinimumSize(320, 240)
        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setToolTip("FPV video. Double-click for full screen.")

    def set_frame(self, rgb: np.ndarray | None) -> None:
        if rgb is None:
            self._frame = None
        else:
            h, w = rgb.shape[:2]
            img = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888)
            self._frame = (img, rgb)          # keep the numpy buffer alive
        self.update()

    def clear(self) -> None:
        self._frame = None
        self.update()

    def current_image(self) -> QtGui.QImage | None:
        return self._frame[0] if self._frame else None

    def set_aspect(self, aspect: float) -> None:
        self.aspect = aspect
        self.update()

    def _target(self) -> QtCore.QRect:
        w, h = self.width(), self.height()
        if w / max(h, 1) > self.aspect:
            tw = int(h * self.aspect)
            return QtCore.QRect((w - tw) // 2, 0, tw, h)
        th = int(w / self.aspect)
        return QtCore.QRect(0, (h - th) // 2, w, th)

    def paintEvent(self, ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtCore.Qt.black)
        t = self._target()
        if self.show_no_signal or self._frame is None:
            p.setPen(QtGui.QColor(TEXT_DIM))
            f = p.font()
            f.setPointSize(max(14, t.height() // 18))
            f.setBold(True)
            p.setFont(f)
            p.drawText(t, QtCore.Qt.AlignCenter, self.no_signal_text if self.show_no_signal or self._frame is None else "")
        else:
            p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
            p.drawImage(t, self._frame[0])
        if self.overlay and self.overlay_lines:
            f = p.font()
            f.setPointSize(9)
            f.setBold(False)
            p.setFont(f)
            fm = QtGui.QFontMetrics(f)
            w = max(fm.horizontalAdvance(s) for s in self.overlay_lines) + 12
            h = fm.height() * len(self.overlay_lines) + 8
            r = QtCore.QRect(t.left() + 6, t.top() + 6, w, h)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor(0, 0, 0, 150))
            p.drawRoundedRect(r, 4, 4)
            p.setPen(QtGui.QColor(TEXT))
            y = r.top() + fm.ascent() + 4
            for s in self.overlay_lines:
                p.drawText(r.left() + 6, y, s)
                y += fm.height()
        p.end()

    def mouseDoubleClickEvent(self, ev):
        self.doubleClicked.emit()

    def keyPressEvent(self, ev):
        if ev.key() == QtCore.Qt.Key_Escape and self.isFullScreen():
            self.doubleClicked.emit()
            return
        super().keyPressEvent(ev)


class FpvDock(QtWidgets.QDockWidget):
    """Dockable, resizable video panel with a full-screen option."""

    def __init__(self, parent=None):
        super().__init__("FPV Video", parent)
        self.setObjectName("fpvdock")
        self.setFeatures(self.DockWidgetMovable | self.DockWidgetFloatable | self.DockWidgetClosable)
        self.view = VideoView()
        self.setWidget(self.view)
        self._fs_window: QtWidgets.QWidget | None = None
        self.view.doubleClicked.connect(self.toggle_fullscreen)

    def toggle_fullscreen(self) -> None:
        if self._fs_window is None:
            win = QtWidgets.QWidget(flags=QtCore.Qt.Window)
            win.setWindowTitle("FPV Video")
            lay = QtWidgets.QVBoxLayout(win)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self.view)
            self._fs_window = win
            win.showFullScreen()
            self.view.setFocus()
        else:
            self._end_fullscreen()

    def _end_fullscreen(self) -> None:
        win = self._fs_window
        self._fs_window = None
        if win is not None:
            self.setWidget(self.view)
            win.close()
            win.deleteLater()

    def is_fullscreen(self) -> bool:
        return self._fs_window is not None


# ============================================================================ FPV Video panel

ASPECTS = [("4:3", 4.0 / 3.0), ("16:9", 16.0 / 9.0)]


class FpvPanel(QtWidgets.QWidget):
    enableRequested = QtCore.pyqtSignal(bool)
    channelSelected = QtCore.pyqtSignal(float)
    paramsChanged = QtCore.pyqtSignal(dict)
    viewChanged = QtCore.pyqtSignal(dict)
    snapshotRequested = QtCore.pyqtSignal()
    recordToggled = QtCore.pyqtSignal(bool)
    showVideoRequested = QtCore.pyqtSignal()
    fullscreenRequested = QtCore.pyqtSignal()
    addChannelRequested = QtCore.pyqtSignal()
    audioChanged = QtCore.pyqtSignal(dict)

    def __init__(self, store: BookmarkStore, parent=None):
        super().__init__(parent)
        self.store = store
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        w, f = form()
        lay.addWidget(w)
        self.enable = QtWidgets.QPushButton("Enter FPV Video mode")
        self.enable.setCheckable(True)
        self.enable.setToolTip("Receive analog FM video: sets 20 MS/s and tunes the HackRF to the channel")
        f.addRow(self.enable)
        self.status = QtWidgets.QLabel("Off")
        self.status.setStyleSheet("color: #8a8f98;")
        self.status.setWordWrap(True)
        f.addRow(self.status)

        self.band = QtWidgets.QComboBox()
        self.channel = QtWidgets.QComboBox()
        rr = QtWidgets.QHBoxLayout()
        rr.addWidget(self.band, 1)
        rr.addWidget(self.channel, 1)
        f.addRow("Channel", rr)
        fr = QtWidgets.QHBoxLayout()
        self.manual = QtWidgets.QLineEdit()
        self.manual.setPlaceholderText("MHz, e.g. 5740")
        self.tune_btn = QtWidgets.QToolButton()
        self.tune_btn.setText("Tune")
        self.tune_btn.setToolTip("Tune to the frequency in the box (MHz, or 5.74G, 1280M, ...)")
        self.add_btn = QtWidgets.QToolButton()
        self.add_btn.setText("+")
        self.add_btn.setToolTip("Save this frequency as a channel in the frequency manager (mode FPV)")
        fr.addWidget(self.manual, 1)
        fr.addWidget(self.tune_btn)
        fr.addWidget(self.add_btn)
        f.addRow("Frequency", fr)

        self.standard = QtWidgets.QComboBox()
        self.standard.addItem("Auto detect", "auto")
        self.standard.addItem("PAL", "PAL")
        self.standard.addItem("NTSC", "NTSC")
        f.addRow("Standard", self.standard)
        self.deinterlace = QtWidgets.QComboBox()
        self.deinterlace.addItem("Weave", "weave")
        self.deinterlace.addItem("Line doubling", "double")
        self.deinterlace.setToolTip("Weave: full vertical resolution (combing on motion).\n"
                                    "Line doubling: one picture per field, less motion blur.")
        f.addRow("Deinterlace", self.deinterlace)
        self.video_bw = ValueSlider(2.0, 6.5, 0.1, 5.0, "MHz", 1)
        self.video_bw.setToolTip("Video low-pass: removes the audio subcarrier and noise above the picture")
        f.addRow("Video BW", self.video_bw)
        self.colour = QtWidgets.QCheckBox("Colour")
        self.colour.setChecked(True)
        self.colour.setToolTip("Falls back to greyscale when no colour burst is found")
        self.saturation = ValueSlider(0.0, 2.0, 0.05, 1.0, "", 2)
        f.addRow(self.colour)
        f.addRow("Saturation", self.saturation)
        self.brightness = ValueSlider(-50, 50, 1, 0, "%")
        self.contrast = ValueSlider(50, 200, 1, 100, "%")
        self.agc = QtWidgets.QCheckBox("Video AGC")
        self.agc.setChecked(True)
        self.agc.setToolTip("Normalise the picture level from the sync amplitude (recommended)")
        self.gain = ValueSlider(-12, 12, 0.5, 0, "dB", 1)
        self.gain.setToolTip("Manual video gain (AGC off), relative to 8 MHz/V deviation")
        f.addRow("Brightness", self.brightness)
        f.addRow("Contrast", self.contrast)
        f.addRow(self.agc)
        f.addRow("Gain", self.gain)
        self.sync_thr = ValueSlider(20, 80, 1, 50, "%")
        self.sync_thr.setToolTip("Sync slicer threshold between the sync tip and blanking level")
        self.h_pos = ValueSlider(-3.0, 3.0, 0.05, 0.0, "µs", 2)
        self.v_pos = ValueSlider(-20, 20, 1, 0, "lines")
        self.h_pos.setToolTip("Picture horizontal position")
        self.v_pos.setToolTip("Picture vertical position (frame lines)")
        f.addRow("Sync threshold", self.sync_thr)
        f.addRow("H position", self.h_pos)
        f.addRow("V position", self.v_pos)
        self.aspect = QtWidgets.QComboBox()
        for name, _ in ASPECTS:
            self.aspect.addItem(name)
        f.addRow("Aspect", self.aspect)
        self.no_signal = QtWidgets.QComboBox()
        self.no_signal.addItem("Show raw (noisy) picture", False)
        self.no_signal.addItem("Show NO SIGNAL screen", True)
        f.addRow("When unlocked", self.no_signal)
        self.overlay = QtWidgets.QCheckBox("Status overlay")
        self.overlay.setChecked(True)
        f.addRow(self.overlay)
        self.audio_on = QtWidgets.QCheckBox("Audio subcarrier")
        self.audio_on.setToolTip("FM audio on a subcarrier of the video, played through the audio output")
        self.subcarrier = QtWidgets.QComboBox()
        for s in SUBCARRIERS:
            self.subcarrier.addItem(f"{s / 1e6:g} MHz", s)
        a_row = QtWidgets.QHBoxLayout()
        a_row.addWidget(self.audio_on, 1)
        a_row.addWidget(self.subcarrier)
        f.addRow(a_row)
        btn = QtWidgets.QHBoxLayout()
        self.video_btn = QtWidgets.QPushButton("Video panel")
        self.video_btn.setToolTip("Show the dockable video panel")
        self.full_btn = QtWidgets.QPushButton("Full screen")
        self.full_btn.setToolTip("Show the video full screen (Esc or double-click returns)")
        btn.addWidget(self.video_btn)
        btn.addWidget(self.full_btn)
        f.addRow(btn)
        rec = QtWidgets.QHBoxLayout()
        self.snapshot_btn = QtWidgets.QPushButton("Snapshot")
        self.snapshot_btn.setToolTip("Save the current decoded frame as PNG to the recording folder")
        self.record_btn = QtWidgets.QPushButton("● Record video")
        self.record_btn.setCheckable(True)
        self.record_btn.setToolTip("Record the decoded video to MP4 in the recording folder")
        rec.addWidget(self.snapshot_btn)
        rec.addWidget(self.record_btn)
        f.addRow(rec)
        self.record_info = QtWidgets.QLabel("")
        self.record_info.setStyleSheet("color: #8a8f98;")
        self.record_info.setWordWrap(True)
        f.addRow(self.record_info)

        self._controls = [self.band, self.channel, self.manual, self.tune_btn, self.add_btn, self.standard,
                          self.deinterlace, self.video_bw, self.colour, self.saturation, self.brightness,
                          self.contrast, self.agc, self.gain, self.sync_thr, self.h_pos, self.v_pos, self.aspect,
                          self.no_signal, self.overlay, self.audio_on, self.subcarrier, self.video_btn, self.full_btn,
                          self.snapshot_btn, self.record_btn]
        self.reload_channels()
        self._sync_enabled(False)

        self.enable.toggled.connect(self.enableRequested)
        self.band.activated.connect(self._band_changed)
        self.channel.activated.connect(self._channel_chosen)
        self.tune_btn.clicked.connect(self._manual_tune)
        self.manual.returnPressed.connect(self._manual_tune)
        self.add_btn.clicked.connect(self.addChannelRequested)
        self.standard.activated.connect(lambda _: self.paramsChanged.emit({"standard": self.standard.currentData()}))
        self.deinterlace.activated.connect(lambda _: self.paramsChanged.emit({"deinterlace": self.deinterlace.currentData()}))
        self.video_bw.valueChanged.connect(lambda v: self.paramsChanged.emit({"video_bw": v * 1e6}))
        self.colour.toggled.connect(lambda on: self.paramsChanged.emit({"colour": on}))
        self.saturation.valueChanged.connect(lambda v: self.paramsChanged.emit({"saturation": v}))
        self.brightness.valueChanged.connect(lambda v: self.paramsChanged.emit({"brightness": v / 100.0}))
        self.contrast.valueChanged.connect(lambda v: self.paramsChanged.emit({"contrast": v / 100.0}))
        self.agc.toggled.connect(self._agc_toggled)
        self.gain.valueChanged.connect(lambda v: self.paramsChanged.emit({"gain_db": v}))
        self.sync_thr.valueChanged.connect(lambda v: self.paramsChanged.emit({"sync_threshold": v / 100.0}))
        self.h_pos.valueChanged.connect(lambda v: self.paramsChanged.emit({"h_shift_us": v}))
        self.v_pos.valueChanged.connect(lambda v: self.paramsChanged.emit({"v_shift": int(v)}))
        self.aspect.activated.connect(lambda i: self.viewChanged.emit({"aspect": ASPECTS[i][1]}))
        self.no_signal.activated.connect(lambda i: self.viewChanged.emit({"no_signal_screen": bool(self.no_signal.itemData(i))}))
        self.overlay.toggled.connect(lambda on: self.viewChanged.emit({"overlay": on}))
        self.audio_on.toggled.connect(lambda on: self.audioChanged.emit({"enabled": on}))
        self.subcarrier.activated.connect(lambda i: self.audioChanged.emit({"subcarrier": float(self.subcarrier.itemData(i))}))
        self.snapshot_btn.clicked.connect(self.snapshotRequested)
        self.record_btn.toggled.connect(self.recordToggled)
        self.video_btn.clicked.connect(self.showVideoRequested)
        self.full_btn.clicked.connect(self.fullscreenRequested)

    # ---- channels
    def reload_channels(self, keep: bool = True) -> None:
        cur_band = self.band.currentData() if keep else None
        cur_ch = self.channel.currentIndex() if keep else 0
        self.band.blockSignals(True)
        self.band.clear()
        for b in BANDS:
            self.band.addItem(f"Band {b}", f"std:{b}")
        for g in user_groups(self.store):
            self.band.addItem(g, f"user:{g}")
        i = self.band.findData(cur_band) if cur_band else -1
        if i < 0:
            i = self.band.findData("std:F")
        self.band.setCurrentIndex(max(i, 0))
        self.band.blockSignals(False)
        self._fill_channels(cur_ch)

    def _fill_channels(self, select: int = 0) -> None:
        kind, _, name = (self.band.currentData() or "std:F").partition(":")
        self.channel.blockSignals(True)
        self.channel.clear()
        if kind == "std":
            for i, f in enumerate(BANDS[name]):
                self.channel.addItem(f"{channel_name(name, i)}  {f} MHz", float(f) * 1e6)
        else:
            for label, hz in group_channels(self.store, name):
                self.channel.addItem(f"{label}  {hz / 1e6:.3f} MHz", hz)
        self.channel.setCurrentIndex(min(max(select, 0), self.channel.count() - 1))
        self.channel.blockSignals(False)

    def _band_changed(self, _=None) -> None:
        self._fill_channels(0)
        self._channel_chosen()

    def _channel_chosen(self, _=None) -> None:
        hz = self.channel.currentData()
        if hz:
            self.channelSelected.emit(float(hz))

    def current_channel(self) -> float | None:
        hz = self.channel.currentData()
        return float(hz) if hz else None

    def select_frequency(self, hz: float) -> None:
        """Show this frequency in the manual box and in the selector if it matches a channel."""
        self.manual.setText(f"{hz / 1e6:.3f}")
        for i in range(self.band.count()):
            kind, _, name = (self.band.itemData(i) or "").partition(":")
            if kind != "std":
                continue
            for j, f in enumerate(BANDS[name]):
                if abs(f * 1e6 - hz) < 5e3:
                    self.band.setCurrentIndex(i)
                    self._fill_channels(j)
                    return

    def _manual_tune(self) -> None:
        hz = parse_frequency(self.manual.text())
        if hz is None or hz <= 0:
            QtWidgets.QToolTip.showText(self.manual.mapToGlobal(QtCore.QPoint(0, 0)), "Enter a frequency, e.g. 5800 or 5.8G")
            return
        self.channelSelected.emit(hz)

    def manual_value(self) -> float | None:
        return parse_frequency(self.manual.text())

    # ---- state
    def _agc_toggled(self, on: bool) -> None:
        self.gain.setEnabled(self.enable.isChecked() and not on)
        self.paramsChanged.emit({"agc": on})

    def _sync_enabled(self, on: bool) -> None:
        for w in self._controls:
            w.setEnabled(on)
        self.gain.setEnabled(on and not self.agc.isChecked())
        self.enable.setText("Leave FPV Video mode" if on else "Enter FPV Video mode")

    def set_active(self, on: bool, note: str = "") -> None:
        self.enable.blockSignals(True)
        self.enable.setChecked(on)
        self.enable.blockSignals(False)
        self._sync_enabled(on)
        self.status.setText(note or ("Receiving" if on else "Off"))

    def set_available(self, ok: bool, why: str = "") -> None:
        self.enable.setEnabled(ok or self.enable.isChecked())
        self.enable.setToolTip(why or "Receive analog FM video: sets 20 MS/s and tunes the HackRF to the channel")

    def apply_settings(self, d: dict) -> None:
        for w in self._controls:
            w.blockSignals(True)
        try:
            band = d.get("band", "std:F")
            if isinstance(band, (list, tuple)):       # older settings
                band = ":".join(band)
            i = self.band.findData(band)
            self.band.setCurrentIndex(max(i, 0))
            self._fill_channels(int(d.get("channel", 0)))
            self.manual.setText(d.get("manual", ""))
            self.standard.setCurrentIndex(max(0, self.standard.findData(d.get("standard", "auto"))))
            self.deinterlace.setCurrentIndex(max(0, self.deinterlace.findData(d.get("deinterlace", "weave"))))
            self.video_bw.setValue(float(d.get("video_bw", 5.0e6)) / 1e6)
            self.colour.setChecked(bool(d.get("colour", True)))
            self.saturation.setValue(float(d.get("saturation", 1.0)))
            self.brightness.setValue(float(d.get("brightness", 0.0)) * 100)
            self.contrast.setValue(float(d.get("contrast", 1.0)) * 100)
            self.agc.setChecked(bool(d.get("agc", True)))
            self.gain.setValue(float(d.get("gain_db", 0.0)))
            self.sync_thr.setValue(float(d.get("sync_threshold", 0.5)) * 100)
            self.h_pos.setValue(float(d.get("h_shift_us", 0.0)))
            self.v_pos.setValue(float(d.get("v_shift", 0)))
            self.aspect.setCurrentIndex(max(0, int(d.get("aspect", 0))))
            self.no_signal.setCurrentIndex(1 if d.get("no_signal_screen", True) else 0)
            self.overlay.setChecked(bool(d.get("overlay", True)))
            self.audio_on.setChecked(bool(d.get("audio", False)))
            self.subcarrier.setCurrentIndex(max(0, self.subcarrier.findData(float(d.get("subcarrier", 6.5e6)))))
        finally:
            for w in self._controls:
                w.blockSignals(False)
        self.gain.setEnabled(self.enable.isChecked() and not self.agc.isChecked())

    def settings(self) -> dict:
        return dict(band=self.band.currentData() or "std:F", channel=self.channel.currentIndex(),
                    manual=self.manual.text(), standard=self.standard.currentData(),
                    deinterlace=self.deinterlace.currentData(), video_bw=self.video_bw.value() * 1e6,
                    colour=self.colour.isChecked(), saturation=self.saturation.value(),
                    brightness=self.brightness.value() / 100.0, contrast=self.contrast.value() / 100.0,
                    agc=self.agc.isChecked(), gain_db=self.gain.value(), sync_threshold=self.sync_thr.value() / 100.0,
                    h_shift_us=self.h_pos.value(), v_shift=int(self.v_pos.value()), aspect=self.aspect.currentIndex(),
                    no_signal_screen=bool(self.no_signal.currentData()), overlay=self.overlay.isChecked(),
                    audio=self.audio_on.isChecked(), subcarrier=float(self.subcarrier.currentData()))

    def decoder_params(self) -> dict:
        s = self.settings()
        return {k: s[k] for k in ("standard", "deinterlace", "video_bw", "colour", "saturation", "brightness",
                                  "contrast", "agc", "gain_db", "sync_threshold", "h_shift_us", "v_shift")}


# ============================================================================ Scanner panel

class FpvScannerPanel(QtWidgets.QWidget):
    scanRequested = QtCore.pyqtSignal(dict)
    stopRequested = QtCore.pyqtSignal()
    tuneRequested = QtCore.pyqtSignal(float)
    settingsChanged = QtCore.pyqtSignal()

    def __init__(self, store: BookmarkStore, parent=None):
        super().__init__(parent)
        self.store = store
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        w, f = form()
        lay.addWidget(w)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItem("Channels (selected bands/groups)", "channels")
        self.mode.addItem("Range in overlapping steps", "range")
        f.addRow("Scan", self.mode)
        self.bands = QtWidgets.QListWidget()
        self.bands.setMaximumHeight(116)
        self.bands.setToolTip("Bands and user groups scanned in channel mode")
        f.addRow(self.bands)
        rr = QtWidgets.QHBoxLayout()
        self.f_lo = QtWidgets.QDoubleSpinBox()
        self.f_hi = QtWidgets.QDoubleSpinBox()
        for sb in (self.f_lo, self.f_hi):
            sb.setRange(1, 6000)
            sb.setDecimals(0)
            sb.setSuffix(" MHz")
            sb.setKeyboardTracking(False)
        self.f_lo.setValue(5000)
        self.f_hi.setValue(6000)
        rr.addWidget(self.f_lo)
        rr.addWidget(QtWidgets.QLabel("to"))
        rr.addWidget(self.f_hi)
        self.range_row = QtWidgets.QWidget()
        self.range_row.setLayout(rr)
        rr.setContentsMargins(0, 0, 0, 0)
        f.addRow("Range", self.range_row)
        opts = QtWidgets.QHBoxLayout()
        self.continuous = QtWidgets.QCheckBox("Continuous")
        self.continuous.setToolTip("Re-scan until stopped")
        self.autotune = QtWidgets.QCheckBox("Auto-tune")
        self.autotune.setToolTip("Tune to the strongest video found when the scan ends")
        opts.addWidget(self.continuous)
        opts.addWidget(self.autotune)
        f.addRow(opts)
        self.scan_btn = QtWidgets.QPushButton("Scan")
        self.scan_btn.setCheckable(True)
        f.addRow(self.scan_btn)
        self.state_label = QtWidgets.QLabel("")
        self.state_label.setStyleSheet("color: #8a8f98;")
        self.state_label.setWordWrap(True)
        f.addRow(self.state_label)
        lay2 = QtWidgets.QVBoxLayout()
        lay2.setContentsMargins(6, 0, 6, 4)
        lay.addLayout(lay2)
        self.results = QtWidgets.QTreeWidget()
        self.results.setHeaderLabels(["MHz", "Channel", "Std", "Level", "Last seen"])
        self.results.setRootIsDecorated(False)
        self.results.setAlternatingRowColors(True)
        self.results.setMinimumHeight(120)
        hdr = self.results.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        for c in (2, 3, 4):
            hdr.setSectionResizeMode(c, QtWidgets.QHeaderView.ResizeToContents)
        lay2.addWidget(self.results)
        self.clear_btn = QtWidgets.QPushButton("Clear results")
        f2 = QtWidgets.QHBoxLayout()
        f2.addStretch(1)
        f2.addWidget(self.clear_btn)
        lay2.addLayout(f2)
        self.reload_groups()
        self._mode_changed()
        self.set_active(False)

        self.mode.activated.connect(lambda _: (self._mode_changed(), self.settingsChanged.emit()))
        self.scan_btn.toggled.connect(self._toggled)
        self.results.itemDoubleClicked.connect(self._double_clicked)
        self.clear_btn.clicked.connect(self._clear)

    def _mode_changed(self):
        ch = self.mode.currentData() == "channels"
        self.bands.setVisible(ch)
        self.range_row.setVisible(not ch)

    def reload_groups(self):
        checked = self.checked_items()
        self.bands.clear()
        for b in BANDS:
            self.bands.addItem(f"Band {b}")
            it = self.bands.item(self.bands.count() - 1)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if f"std:{b}" in (checked or [f"std:{x}" for x in BANDS]) else QtCore.Qt.Unchecked)
            it.setData(QtCore.Qt.UserRole, f"std:{b}")
        for g in user_groups(self.store):
            self.bands.addItem(g)
            it = self.bands.item(self.bands.count() - 1)
            it.setFlags(it.flags() | QtCore.Qt.ItemIsUserCheckable)
            it.setCheckState(QtCore.Qt.Checked if checked and f"user:{g}" in checked else QtCore.Qt.Unchecked)
            it.setData(QtCore.Qt.UserRole, f"user:{g}")

    def checked_items(self):
        out = []
        for i in range(self.bands.count()):
            it = self.bands.item(i)
            if it.checkState() == QtCore.Qt.Checked:
                out.append(it.data(QtCore.Qt.UserRole))
        return out

    def set_active(self, on: bool) -> None:
        self.setEnabled(on)

    def set_available(self, ok: bool, why: str = "") -> None:
        self.scan_btn.setEnabled(ok)
        self.scan_btn.setToolTip(why)
        if not ok:
            self.scan_btn.setChecked(False)

    def _toggled(self, on: bool) -> None:
        self.scan_btn.setText("Stop scan" if on else "Scan")
        if on:
            d = dict(mode=self.mode.currentData(), bands=self.checked_items(), f_lo=self.f_lo.value() * 1e6,
                     f_hi=self.f_hi.value() * 1e6, continuous=self.continuous.isChecked())
            self.scanRequested.emit(d)
        else:
            self.stopRequested.emit()

    def set_running(self, on: bool) -> None:
        self.scan_btn.blockSignals(True)
        self.scan_btn.setChecked(on)
        self.scan_btn.setText("Stop scan" if on else "Scan")
        self.scan_btn.blockSignals(False)

    def set_results(self, results) -> None:
        self.results.clear()
        now = time.time()
        for r in sorted(results, key=lambda x: x.frequency):
            ago = now - r.last_seen
            seen = "now" if ago < 1.5 else f"{ago:.0f} s"
            it = QtWidgets.QTreeWidgetItem([f"{r.frequency / 1e6:.2f}", r.channel, r.standard,
                                            f"{r.level_db:.0f} dBFS", seen])
            it.setData(0, QtCore.Qt.UserRole, r.frequency)
            self.results.addTopLevelItem(it)

    def _double_clicked(self, it, _col=0):
        hz = it.data(0, QtCore.Qt.UserRole)
        if hz:
            self.tuneRequested.emit(float(hz))

    def _clear(self):
        self.results.clear()

    def settings(self) -> dict:
        return dict(mode=self.mode.currentData(), bands=self.checked_items(),
                    f_lo=self.f_lo.value(), f_hi=self.f_hi.value(), continuous=self.continuous.isChecked(),
                    autotune=self.autotune.isChecked())

    def apply_settings(self, d: dict) -> None:
        self.mode.setCurrentIndex(max(0, self.mode.findData(d.get("mode", "channels"))))
        self.f_lo.setValue(float(d.get("f_lo", 5000)))
        self.f_hi.setValue(float(d.get("f_hi", 6000)))
        self.continuous.setChecked(bool(d.get("continuous", False)))
        self.autotune.setChecked(bool(d.get("autotune", False)))
        bands = {(":".join(b) if isinstance(b, (list, tuple)) else b) for b in d.get("bands", [])}
        if bands:
            for i in range(self.bands.count()):
                it = self.bands.item(i)
                it.setCheckState(QtCore.Qt.Checked if it.data(QtCore.Qt.UserRole) in bands else QtCore.Qt.Unchecked)
        self._mode_changed()
