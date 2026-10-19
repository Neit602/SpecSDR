"""Spectrum + waterfall display (pyqtgraph), sharing one frequency axis.

X coordinates are Hz *relative to the current centre frequency* (`ref`); the axis adds
`ref` back when labelling. This keeps coordinates small (no float precision trouble at
GHz) and makes retuning cheap.

Waterfall: a custom GraphicsObject owning a ring-buffer QImage (one row per line, newest
at the top). A new line costs one row of colour lookup; drawing is two drawImage() calls
that Qt scales in C++. See DECISIONS.md for the measurement vs. pyqtgraph ImageItem."""

from __future__ import annotations

import math
import time

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

from ..dsp.spectrum import SpectrumDisplay, SpectrumFrame
from . import theme

WF_MAX_COLS = 8192
WF_ROWS = 1024


def format_freq(hz: float, decimals: int = 6) -> str:
    return f"{hz / 1e6:.{decimals}f}"


class FreqAxis(pg.AxisItem):
    """Bottom axis: values are offsets from `ref`; ticks are placed on round absolute
    frequencies and labelled in MHz."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.ref = 0.0

    def tickValues(self, minVal, maxVal, size):
        levels = super().tickValues(minVal + self.ref, maxVal + self.ref, size)
        return [(sp, [v - self.ref for v in vals]) for sp, vals in levels]

    def tickStrings(self, values, scale, spacing):
        dec = max(0, int(math.ceil(-math.log10(max(spacing, 1e-3) / 1e6))))
        return [format_freq(v + self.ref, min(dec, 6)) for v in values]

    def label_for(self, offset: float, spacing: float) -> str:
        return self.tickStrings([offset], 1.0, spacing)[0]


class TuneViewBox(pg.ViewBox):
    """ViewBox with the default pan/zoom replaced by tuning interactions."""

    def __init__(self, panel: "SpectrumPanel"):
        super().__init__(enableMenu=False)
        self.panel = panel
        self.setMouseEnabled(False, False)
        self._drag = None
        self.setAcceptHoverEvents(True)

    def _x(self, scene_pos) -> float:
        return self.mapSceneToView(scene_pos).x()

    def mouseClickEvent(self, ev):
        if ev.button() == QtCore.Qt.LeftButton:
            ev.accept()
            self.panel._on_click(self._x(ev.scenePos()))
        else:
            ev.ignore()

    def mouseDragEvent(self, ev, axis=None):
        ev.accept()
        if ev.button() == QtCore.Qt.LeftButton:
            if ev.isStart():
                self._drag = self.panel._hit_test(self, ev.buttonDownScenePos())
                self.panel._drag_start(self._drag, self._x(ev.buttonDownScenePos()))
            if self._drag:
                self.panel._drag_move(self._drag, self._x(ev.scenePos()), ev.isFinish())
            if ev.isFinish():
                self._drag = None
        elif ev.button() in (QtCore.Qt.RightButton, QtCore.Qt.MiddleButton):
            dx = self._x(ev.lastScenePos()) - self._x(ev.scenePos())
            self.panel._pan(dx)

    def wheelEvent(self, ev, axis=None):
        ev.accept()
        steps = ev.delta() / 120.0
        self.panel._on_wheel(steps, ev.modifiers(), self._x(ev.scenePos()))

    def hoverEvent(self, ev):
        if ev.isExit():
            self.panel._on_hover(self, None)
            return
        self.panel._on_hover(self, ev.scenePos())


class WaterfallItem(pg.GraphicsObject):
    def __init__(self):
        super().__init__()
        self.cols = 0
        self.rows = WF_ROWS
        self.head = 0
        self.count = 0
        self.img: QtGui.QImage | None = None
        self.pix: np.ndarray | None = None      # uint32 view of the QImage
        self.hist: np.ndarray | None = None     # dB values (float16 to halve memory)
        self.x0 = 0.0                           # left edge (relative Hz) of column 0
        self.width_hz = 1.0
        self.lut = theme.colormap_lut("classic")
        self.lo_db, self.hi_db, self.gamma = -120.0, 0.0, 1.0
        self.key = None                         # (fft size, sample rate) the history belongs to
        self.center = None                      # centre frequency the columns are aligned to

    # ---------------------------------------------------------------- data
    def _alloc(self, cols: int):
        self.cols = cols
        self.img = QtGui.QImage(cols, self.rows, QtGui.QImage.Format_RGB32)
        self.img.fill(QtGui.QColor(0, 0, 0))
        ptr = self.img.bits()
        ptr.setsize(self.img.byteCount())
        self.pix = np.frombuffer(ptr, np.uint32).reshape(self.rows, cols)
        self.hist = np.full((self.rows, cols), -250, np.float16)
        self.head = 0
        self.count = 0

    def clear(self):
        self.key = None
        self.center = None
        if self.hist is not None:
            self.hist[:] = -250
            self.pix[:] = 0xFF000000
            self.count = 0
        self.update()

    def _colorize(self, db: np.ndarray) -> np.ndarray:
        v = (db.astype(np.float32) - self.lo_db) / max(self.hi_db - self.lo_db, 1e-3)
        np.clip(v, 0.0, 1.0, out=v)
        if self.gamma != 1.0:
            v **= self.gamma
        return self.lut[(v * (len(self.lut) - 1)).astype(np.int32)]

    def recolor(self):
        if self.pix is not None:
            self.pix[:] = self._colorize(self.hist)
            self.update()

    def set_levels(self, lo, hi, gamma):
        if (lo, hi, gamma) != (self.lo_db, self.hi_db, self.gamma):
            self.lo_db, self.hi_db, self.gamma = lo, hi, gamma
            self.recolor()

    def set_colormap(self, name):
        self.lut = theme.colormap_lut(name)
        self.recolor()

    def add_frame(self, frame: SpectrumFrame, ref: float):
        n = frame.size
        cols = min(n, WF_MAX_COLS)
        key = (n, frame.sample_rate)
        if self.cols != cols or self.img is None:
            self._alloc(cols)
            self.key = key
        if key != self.key:
            self.clear()
            self.key = key
        if self.center is None:
            self.center = frame.center
        elif frame.center != self.center:
            # shift history so old lines stay at their absolute frequencies
            colw = frame.sample_rate / cols
            shift = int(round((self.center - frame.center) / colw))
            self.center = frame.center
            if abs(shift) >= cols:
                self.hist[:] = -250
            elif shift:
                self.hist[:] = np.roll(self.hist, shift, axis=1)
                if shift > 0:
                    self.hist[:, :shift] = -250
                else:
                    self.hist[:, shift:] = -250
            self.recolor()
        m = n // cols
        row = frame.db.reshape(cols, m).max(axis=1) if m > 1 else frame.db
        self.head = (self.head - 1) % self.rows
        self.hist[self.head] = row
        self.pix[self.head] = self._colorize(row)
        self.count = min(self.count + 1, self.rows)
        binw = frame.sample_rate / n
        self.prepareGeometryChange()
        self.x0 = frame.center - ref + (-(n // 2) - 0.5) * binw
        self.width_hz = n * binw
        self.update()

    # ---------------------------------------------------------------- painting
    def boundingRect(self):
        return QtCore.QRectF(self.x0, 0, self.width_hz, self.rows)

    def paint(self, p, *args):
        if self.img is None:
            return
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, False)
        h1 = self.rows - self.head  # rows from head to the end of the image
        colw = self.width_hz / self.cols
        # Only draw the visible columns. Handing Qt the whole (much wider than the viewport,
        # when zoomed) image made its clipping path smear the first visible columns.
        c0, c1 = 0, self.cols
        vb = self.getViewBox()
        if vb is not None:
            (vx0, vx1), _ = vb.viewRange()
            c0 = max(0, int(math.floor((vx0 - self.x0) / colw)) - 1)
            c1 = min(self.cols, int(math.ceil((vx1 - self.x0) / colw)) + 1)
            if c1 <= c0:
                return
        tx = self.x0 + c0 * colw
        tw = (c1 - c0) * colw
        # newest line (at image row `head`) is drawn at y = 0 (top)
        p.drawImage(QtCore.QRectF(tx, 0, tw, h1), self.img, QtCore.QRectF(c0, self.head, c1 - c0, h1))
        if self.head:
            p.drawImage(QtCore.QRectF(tx, h1, tw, self.head), self.img,
                        QtCore.QRectF(c0, 0, c1 - c0, self.head))


class SpectrumPanel(QtWidgets.QWidget):
    tuneRequested = QtCore.pyqtSignal(float)        # absolute Hz from a click (controller snaps)
    vfoDragged = QtCore.pyqtSignal(float)           # absolute Hz while dragging the passband
    bandwidthRequested = QtCore.pyqtSignal(float)   # Hz, from dragging a passband edge
    wheelTune = QtCore.pyqtSignal(int)              # +/- steps
    zoomChanged = QtCore.pyqtSignal(float)

    LEFT_AXIS_W = 52
    EDGE_PX = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=False, background=theme.BG, foreground=theme.TEXT_DIM)
        self.ref = 100e6           # centre frequency the x axis is relative to
        self.sample_rate = 2.4e6
        self.vfo = 100e6
        self.pb_lo, self.pb_hi = -6250.0, 6250.0
        self.sideband = "both"
        self.zoom = 1.0
        self.view_center = 0.0     # relative Hz
        self.display = SpectrumDisplay()
        self.fill = True
        self._frame: SpectrumFrame | None = None
        self._fps_n = 0
        self._fps_t = time.monotonic()
        self.fps = 0.0
        self._drag_ref = None
        self._hover_x = None

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        lay.addWidget(self.splitter)

        # ---- spectrum
        self.spec_vb = TuneViewBox(self)
        self.freq_axis = FreqAxis("bottom")
        self.freq_axis.enableAutoSIPrefix(False)
        self.freq_axis.ref = self.ref
        self.freq_axis.setLabel("MHz")
        self.spec_w = pg.PlotWidget(viewBox=self.spec_vb, axisItems={"bottom": self.freq_axis})
        self.spec_plot = self.spec_w.getPlotItem()
        self.spec_plot.hideButtons()
        self.spec_plot.showGrid(x=True, y=True, alpha=0.18)
        self.spec_plot.getAxis("left").setWidth(self.LEFT_AXIS_W)
        self.spec_plot.getAxis("left").setLabel("dBFS")
        self.spec_plot.getAxis("left").enableAutoSIPrefix(False)
        self.spec_plot.setMenuEnabled(False)
        grad = QtGui.QLinearGradient(0, 0, 0, 1)
        grad.setCoordinateMode(QtGui.QGradient.ObjectBoundingMode)
        grad.setColorAt(0.0, QtGui.QColor(*theme.SPECTRUM_FILL_TOP))
        grad.setColorAt(1.0, QtGui.QColor(*theme.SPECTRUM_FILL_BOTTOM))
        self._fill_brush = QtGui.QBrush(grad)
        self.curve = pg.PlotCurveItem(pen=pg.mkPen(theme.SPECTRUM_LINE, width=1))
        self.peak_curve = pg.PlotCurveItem(pen=pg.mkPen(theme.PEAK_LINE, width=1))
        self.peak_curve.setVisible(False)
        self.spec_plot.addItem(self.curve)
        self.spec_plot.addItem(self.peak_curve)
        self.pb_spec = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(*theme.PASSBAND),
                                           pen=pg.mkPen((120, 180, 255, 160)))
        self.vfo_spec = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(theme.VFO, width=1))
        self.spec_plot.addItem(self.pb_spec)
        self.spec_plot.addItem(self.vfo_spec)
        self.readout = pg.TextItem("", color=theme.TEXT, anchor=(1, 0))
        self.readout.setZValue(20)
        self.spec_plot.addItem(self.readout, ignoreBounds=True)
        self.splitter.addWidget(self.spec_w)

        # ---- waterfall
        self.wf_vb = TuneViewBox(self)
        self.wf_w = pg.PlotWidget(viewBox=self.wf_vb)
        self.wf_plot = self.wf_w.getPlotItem()
        self.wf_plot.hideButtons()
        self.wf_plot.setMenuEnabled(False)
        self.wf_plot.hideAxis("bottom")
        self.wf_plot.getAxis("left").setWidth(self.LEFT_AXIS_W)
        self.wf_plot.getAxis("left").setStyle(showValues=False)
        self.wf_vb.invertY(True)
        self.wf_item = WaterfallItem()
        self.wf_plot.addItem(self.wf_item)
        self.pb_wf = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(61, 142, 230, 35),
                                         pen=pg.mkPen((120, 180, 255, 110)))
        self.vfo_wf = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(theme.VFO, width=1))
        self.wf_plot.addItem(self.pb_wf)
        self.wf_plot.addItem(self.vfo_wf)
        self.wf_vb.setXLink(self.spec_vb)
        self.splitter.addWidget(self.wf_w)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.wf_vb.sigResized.connect(self._wf_resized)

        self.set_levels(0.0, 110.0)
        self.wf_gamma = 1.0
        self._apply_view()
        self._update_overlay()

    def shutdown(self) -> None:
        """Clear the pyqtgraph scenes before Qt deletes the widgets. Without this a scene
        update still queued at teardown can reach items whose ViewBox is already gone
        (seen as an AttributeError in InfiniteLine.boundingRect, then a segfault)."""
        for w in (self.spec_w, self.wf_w):
            if getattr(w, "plotItem", None) is None:
                continue  # already closed
            try:
                w.close()
            except RuntimeError:  # already deleted on the C++ side
                pass

    def closeEvent(self, ev):
        self.shutdown()
        super().closeEvent(ev)

    # ================================================================ public API
    def set_frames(self, frames: list[SpectrumFrame], draw: bool = True) -> None:
        """Add waterfall lines for all frames; smooth every frame; redraw the trace if `draw`."""
        if not frames:
            return
        for fr in frames:
            if fr.waterfall:
                self.wf_item.add_frame(fr, self.ref)
            cur = self.display.update(fr)
        self._frame = frames[-1]
        if not draw:
            return
        self._draw(self._frame, cur)
        self._fps_n += 1
        now = time.monotonic()
        if now - self._fps_t >= 1.0:
            self.fps = self._fps_n / (now - self._fps_t)
            self._fps_n = 0
            self._fps_t = now

    def set_center(self, hz: float) -> None:
        if hz == self.ref:
            return
        old = self.ref
        self.ref = float(hz)
        self.freq_axis.ref = self.ref
        # the axis caches its labels and only redraws on range changes, which may not happen here
        self.freq_axis.picture = None
        self.freq_axis.update()
        # keep the view on the same absolute frequencies when possible
        self.view_center += old - self.ref
        self._apply_view()
        self._update_overlay()
        self.wf_item.prepareGeometryChange()
        self.wf_item.x0 += old - self.ref
        self.wf_item.update()

    def set_sample_rate(self, hz: float) -> None:
        if hz != self.sample_rate:
            self.sample_rate = float(hz)
            self.display.reset()
            self.wf_item.clear()
            self._apply_view()

    def set_vfo(self, hz: float, ensure_visible: bool = True) -> None:
        self.vfo = float(hz)
        if ensure_visible:
            x = self.vfo - self.ref
            half = self._view_width() / 2
            if abs(x - self.view_center) > half * 0.95:
                self.view_center = x
                self._apply_view()
        self._update_overlay()

    def set_passband(self, lo: float, hi: float, sideband: str) -> None:
        self.pb_lo, self.pb_hi, self.sideband = lo, hi, sideband
        self._update_overlay()

    def set_levels(self, ref_db: float, range_db: float) -> None:
        self.ref_db, self.range_db = ref_db, max(range_db, 5.0)
        lo = self.ref_db - self.range_db
        self.spec_vb.setYRange(lo, self.ref_db, padding=0)
        self.wf_item.set_levels(lo, self.ref_db, getattr(self, "wf_gamma", 1.0))
        self._place_readout()

    def set_wf_contrast(self, contrast: float) -> None:
        """contrast -100..100 -> gamma 4..0.25 applied to the normalised level."""
        self.wf_gamma = 2 ** (-contrast / 50.0)
        self.wf_item.set_levels(self.ref_db - self.range_db, self.ref_db, self.wf_gamma)

    def set_colormap(self, name: str) -> None:
        self.wf_item.set_colormap(name)

    def set_fill(self, on: bool) -> None:
        self.fill = on
        if self._frame is not None:
            self._draw(self._frame, self.display.current)

    def set_smoothing(self, s: float) -> None:
        self.display.smoothing = s

    def set_peak_hold(self, on: bool) -> None:
        self.display.peak_hold = on
        if on and self.display.current is not None:
            self.display.peak = self.display.current.copy()
        self.peak_curve.setVisible(on)

    def set_zoom(self, z: float, around: float | None = None) -> None:
        z = float(min(max(z, 1.0), self.max_zoom()))
        if around is None:
            vx = self.vfo - self.ref
            around = vx if abs(vx) <= self.sample_rate / 2 else self.view_center
        old_w = self._view_width()
        self.zoom = z
        new_w = self._view_width()
        # keep `around` at the same relative screen position
        frac = 0.5 if old_w <= 0 else (around - (self.view_center - old_w / 2)) / old_w
        self.view_center = around - new_w * (frac - 0.5)
        self._apply_view()
        self.zoomChanged.emit(self.zoom)

    def max_zoom(self) -> float:
        n = self._frame.size if self._frame is not None else 8192
        return max(1.0, n / 64.0)

    def x_to_freq(self, x: float) -> float:
        return self.ref + x

    def freq_to_x(self, hz: float) -> float:
        return hz - self.ref

    # ================================================================ drawing
    def _view_width(self) -> float:
        return self.sample_rate / self.zoom

    def _apply_view(self) -> None:
        w = self._view_width()
        half_span = self.sample_rate / 2
        c = min(max(self.view_center, -half_span + w / 2), half_span - w / 2)
        self.view_center = c
        self.spec_vb.setXRange(c - w / 2, c + w / 2, padding=0)
        self._place_readout()
        if self._frame is not None and self.display.current is not None:
            self._draw(self._frame, self.display.current)

    def _wf_resized(self) -> None:
        h = max(10, int(self.wf_vb.height()))
        self.wf_vb.setYRange(0, min(h, WF_ROWS), padding=0)

    def _visible_points(self, fr: SpectrumFrame, y: np.ndarray):
        n = fr.size
        binw = fr.sample_rate / n
        (x0, x1), _ = self.spec_vb.viewRange()
        off = fr.center - self.ref
        k0 = int(math.floor((x0 - off) / binw + n // 2)) - 1
        k1 = int(math.ceil((x1 - off) / binw + n // 2)) + 2
        k0, k1 = max(0, k0), min(n, k1)
        if k1 - k0 < 2:
            return np.zeros(0), np.zeros(0)
        px = max(64, int(self.spec_vb.width()))
        seg = y[k0:k1]
        m = (k1 - k0) // px
        if m >= 2:
            cnt = (k1 - k0) // m
            seg = seg[:cnt * m].reshape(cnt, m).max(axis=1)
            ks = k0 + (np.arange(cnt) + 0.5) * m - 0.5
        else:
            ks = np.arange(k0, k1, dtype=np.float64)
        xs = off + (ks - n // 2) * binw
        return xs, seg

    def _draw(self, fr: SpectrumFrame, cur: np.ndarray) -> None:
        xs, ys = self._visible_points(fr, cur)
        if self.fill:
            self.curve.setFillLevel(self.ref_db - self.range_db - 10)
            self.curve.setBrush(self._fill_brush)
        else:
            self.curve.setFillLevel(None)
            self.curve.setBrush(None)
        self.curve.setData(xs, ys)
        if self.display.peak_hold and self.display.peak is not None:
            px, py = self._visible_points(fr, self.display.peak)
            self.peak_curve.setData(px, py)

    def _update_overlay(self) -> None:
        x = self.vfo - self.ref
        for vfo_line in (self.vfo_spec, self.vfo_wf):
            vfo_line.setValue(x)
        for pb in (self.pb_spec, self.pb_wf):
            pb.setRegion((x + self.pb_lo, x + self.pb_hi))

    def _place_readout(self) -> None:
        (x0, x1), _ = self.spec_vb.viewRange()
        self.readout.setPos(x1, self.ref_db)

    # ================================================================ interaction
    def _px_dist(self, vb: pg.ViewBox, x_view: float, scene_pos) -> float:
        p = vb.mapViewToScene(QtCore.QPointF(x_view, 0))
        return abs(p.x() - scene_pos.x())

    def _hit_test(self, vb, scene_pos) -> str | None:
        x_vfo = self.vfo - self.ref
        lo, hi = x_vfo + self.pb_lo, x_vfo + self.pb_hi
        d_lo = self._px_dist(vb, lo, scene_pos)
        d_hi = self._px_dist(vb, hi, scene_pos)
        can_lo = self.sideband in ("both", "lower")
        can_hi = self.sideband in ("both", "upper")
        if can_hi and d_hi <= self.EDGE_PX and (d_hi <= d_lo or not can_lo):
            return "hi"
        if can_lo and d_lo <= self.EDGE_PX:
            return "lo"
        x = vb.mapSceneToView(scene_pos).x()
        if lo <= x <= hi or min(d_lo, d_hi) <= self.EDGE_PX:
            return "move"
        return "pan"

    def _drag_start(self, mode, x):
        self._drag_ref = (x, self.vfo)

    def _drag_move(self, mode, x, finished):
        if mode == "move":
            x_start, vfo_start = self._drag_ref
            self.vfoDragged.emit(vfo_start + (x - x_start))
        elif mode == "hi":
            xv = self.vfo - self.ref
            bw = 2 * (x - xv) if self.sideband == "both" else (x - xv)
            self.bandwidthRequested.emit(abs(bw))
        elif mode == "lo":
            xv = self.vfo - self.ref
            bw = 2 * (xv - x) if self.sideband == "both" else (xv - x)
            self.bandwidthRequested.emit(abs(bw))
        elif mode == "pan":
            x_start, _ = self._drag_ref
            # dragging empty spectrum with the left button pans when zoomed in
            if self.zoom > 1.0:
                self._pan(x_start - x)

    def _pan(self, dx: float) -> None:
        if self.zoom <= 1.0:
            return
        self.view_center += dx
        self._apply_view()

    def _on_click(self, x: float) -> None:
        self.tuneRequested.emit(self.ref + x)

    def _on_wheel(self, steps: float, mods, x: float) -> None:
        if mods & QtCore.Qt.ControlModifier:
            self.set_zoom(self.zoom * (1.25 ** steps), around=x)
        else:
            s = int(round(steps)) or (1 if steps > 0 else -1)
            self.wheelTune.emit(s)

    def _on_hover(self, vb, scene_pos) -> None:
        if scene_pos is None:
            self.readout.setText("")
            for v in (self.spec_vb, self.wf_vb):
                v.setCursor(QtCore.Qt.CrossCursor)
            return
        mode = self._hit_test(vb, scene_pos)
        cur = {"hi": QtCore.Qt.SizeHorCursor, "lo": QtCore.Qt.SizeHorCursor,
               "move": QtCore.Qt.SizeAllCursor}.get(mode, QtCore.Qt.CrossCursor)
        vb.setCursor(cur)
        x = vb.mapSceneToView(scene_pos).x()
        txt = f"{format_freq(self.ref + x, 4)} MHz"
        fr = self._frame
        if fr is not None and self.display.current is not None:
            k = int(round(fr.frequency_to_bin(self.ref + x)))
            if 0 <= k < fr.size:
                txt += f"   {self.display.current[k]:.1f} dBFS"
        self.readout.setText(txt)
