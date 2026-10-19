"""Reusable widgets: collapsible sections, labelled sliders, and the form builder that
turns a source's declarative Control list into a settings panel."""

from __future__ import annotations

import logging

from PyQt5 import QtCore, QtGui, QtWidgets

from ..sources.base import Control, Source, SourceError

log = logging.getLogger(__name__)

NOT_IMPLEMENTED = "Not implemented"


class CollapsibleSection(QtWidgets.QWidget):
    toggled = QtCore.pyqtSignal(bool)

    def __init__(self, title: str, content: QtWidgets.QWidget, expanded: bool = True, parent=None):
        super().__init__(parent)
        self.title = title
        self.header = QtWidgets.QPushButton()
        self.header.setObjectName("sectionHeader")
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setCursor(QtCore.Qt.PointingHandCursor)
        self.content = content
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 2)
        lay.setSpacing(0)
        lay.addWidget(self.header)
        lay.addWidget(content)
        self.header.toggled.connect(self._toggle)
        self._toggle(expanded)

    def _toggle(self, on: bool):
        self.header.setText(("▾  " if on else "▸  ") + self.title)
        self.content.setVisible(on)
        self.toggled.emit(on)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, on: bool):
        self.header.setChecked(on)


class SidePanel(QtWidgets.QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        inner = QtWidgets.QWidget()
        self._lay = QtWidgets.QVBoxLayout(inner)
        self._lay.setContentsMargins(2, 2, 2, 2)
        self._lay.setSpacing(2)
        self._lay.addStretch(1)
        self.setWidget(inner)
        self.sections: dict[str, CollapsibleSection] = {}
        self.setMinimumWidth(250)

    def add_section(self, key: str, title: str, widget: QtWidgets.QWidget, expanded=True) -> CollapsibleSection:
        sec = CollapsibleSection(title, widget, expanded)
        self._lay.insertWidget(self._lay.count() - 1, sec)
        self.sections[key] = sec
        return sec


def form() -> tuple[QtWidgets.QWidget, QtWidgets.QFormLayout]:
    w = QtWidgets.QWidget()
    f = QtWidgets.QFormLayout(w)
    f.setContentsMargins(8, 4, 8, 6)
    f.setHorizontalSpacing(8)
    f.setVerticalSpacing(4)
    f.setFieldGrowthPolicy(QtWidgets.QFormLayout.AllNonFixedFieldsGrow)
    return w, f


class ValueSlider(QtWidgets.QWidget):
    """Horizontal slider with a value label. Works on floats (min, max, step) or on a list
    of discrete values."""

    valueChanged = QtCore.pyqtSignal(float)

    def __init__(self, minimum=0.0, maximum=100.0, step=1.0, value=None, unit="", decimals=0,
                 values: list | None = None, fmt=None, parent=None):
        super().__init__(parent)
        self.values = list(values) if values else None
        self.minimum, self.maximum, self.step = float(minimum), float(maximum), float(step or 1)
        self.unit, self.decimals, self.fmt = unit, decimals, fmt
        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        n = len(self.values) - 1 if self.values else int(round((self.maximum - self.minimum) / self.step))
        self.slider.setRange(0, max(n, 0))
        self.slider.setPageStep(max(1, n // 10))
        self.label = QtWidgets.QLabel()
        self.label.setMinimumWidth(QtGui.QFontMetrics(self.label.font()).horizontalAdvance("-000.0 dBFS") )
        self.label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.label)
        self.slider.valueChanged.connect(self._changed)
        self.setValue(value if value is not None else (self.values[0] if self.values else self.minimum))

    def _pos_value(self, pos: int) -> float:
        if self.values:
            return float(self.values[pos])
        return self.minimum + pos * self.step

    def value(self) -> float:
        return self._pos_value(self.slider.value())

    def setValue(self, v: float) -> None:
        if self.values:
            pos = min(range(len(self.values)), key=lambda i: abs(float(self.values[i]) - float(v)))
        else:
            pos = int(round((float(v) - self.minimum) / self.step))
        self.slider.blockSignals(True)
        self.slider.setValue(pos)
        self.slider.blockSignals(False)
        self._show(self.value())

    def _show(self, v):
        txt = self.fmt(v) if self.fmt else f"{v:.{self.decimals}f}{(' ' + self.unit) if self.unit else ''}"
        self.label.setText(txt)

    def _changed(self, pos):
        v = self._pos_value(pos)
        self._show(v)
        self.valueChanged.emit(v)


class ControlsForm(QtWidgets.QWidget):
    """Builds widgets for a Source's declarative controls and keeps them in sync."""

    changed = QtCore.pyqtSignal(str, object)

    def __init__(self, source: Source, parent=None, include_groups: bool = True):
        super().__init__(parent)
        self.source = source
        self.widgets: dict[str, QtWidgets.QWidget] = {}
        self.specs: dict[str, Control] = {}
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        groups: dict[str, QtWidgets.QFormLayout] = {}
        top_w, top = form()
        outer.addWidget(top_w)
        omitted = False
        for c in source.controls():
            if c.group and not include_groups:
                omitted = True
                continue
            self.specs[c.key] = c
            lay = top
            if c.group:
                if c.group not in groups:
                    box = QtWidgets.QGroupBox(c.group)
                    gl = QtWidgets.QFormLayout(box)
                    gl.setContentsMargins(6, 6, 6, 4)
                    gl.setVerticalSpacing(3)
                    outer.addWidget(box)
                    groups[c.group] = gl
                lay = groups[c.group]
            w = self._make(c)
            if w is None:
                continue
            if c.tooltip:
                w.setToolTip(c.tooltip)
            self.widgets[c.key] = w
            if c.kind == "bool":
                lay.addRow(w)
            else:
                lay.addRow(c.label, w)
        if omitted:
            hint = QtWidgets.QLabel("More settings: ⚙ button in the toolbar")
            hint.setStyleSheet("color: #8a8f98;")
            top.addRow(hint)
        outer.addStretch(1)
        self.refresh()

    def _make(self, c: Control):
        v = self.source.get_control(c.key)
        if c.kind == "info":
            lab = QtWidgets.QLabel(self.source.info_text() or "-")
            lab.setWordWrap(True)
            lab.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            return lab
        if c.kind == "bool":
            w = QtWidgets.QCheckBox(c.label)
            w.setChecked(bool(v))
            w.toggled.connect(lambda on, k=c.key: self._set(k, on))
            return w
        if c.kind == "choice":
            w = QtWidgets.QComboBox()
            for val, label in c.choices or []:
                w.addItem(label, val)
            w.currentIndexChanged.connect(lambda i, k=c.key, cb=w: self._set(k, cb.itemData(i)))
            return w
        if c.kind == "slider":
            vals = [cv for cv, _ in c.choices] if c.choices else None
            w = ValueSlider(c.minimum or 0, c.maximum or 100, c.step or 1, v, c.unit, c.decimals, values=vals,
                            fmt=(lambda x, ch=dict(c.choices): ch.get(x, f"{x:g}")) if c.choices else None)
            w.valueChanged.connect(lambda x, k=c.key: self._set(k, x))
            return w
        if c.kind == "number":
            if c.decimals:
                w = QtWidgets.QDoubleSpinBox()
                w.setDecimals(c.decimals)
            else:
                w = QtWidgets.QSpinBox()
            w.setRange(c.minimum if c.minimum is not None else -1e9, c.maximum if c.maximum is not None else 1e9)
            w.setSingleStep(c.step or 1)
            if c.unit:
                w.setSuffix(" " + c.unit)
            w.setKeyboardTracking(False)
            w.valueChanged.connect(lambda x, k=c.key: self._set(k, x))
            return w
        log.warning("unknown control kind %s", c.kind)
        return None

    def _set(self, key: str, value):
        c = self.specs[key]
        if c.confirm and value and not self.source.get_control(key):
            r = QtWidgets.QMessageBox.warning(self, c.label, c.confirm,
                                              QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
                                              QtWidgets.QMessageBox.Cancel)
            if r != QtWidgets.QMessageBox.Yes:
                self.refresh()
                return
        try:
            self.source.set_control(key, value)
            self.changed.emit(key, value)
        except (SourceError, Exception) as e:
            QtWidgets.QMessageBox.warning(self, c.label, str(e))
        self.refresh()

    def refresh(self):
        """Pull values from the source and update enabled states."""
        for key, w in self.widgets.items():
            c = self.specs[key]
            v = self.source.get_control(key)
            w.blockSignals(True)
            if c.kind == "info":
                w.setText(self.source.info_text() or "-")
            elif c.kind == "bool":
                w.setChecked(bool(v))
            elif c.kind == "choice":
                i = w.findData(v)
                if i < 0 and v is not None:
                    i = next((j for j in range(w.count()) if str(w.itemData(j)) == str(v)), -1)
                if i >= 0:
                    w.setCurrentIndex(i)
            elif c.kind == "slider":
                if v is not None:
                    w.setValue(v)
            elif c.kind == "number":
                if v is not None:
                    w.setValue(v)
            w.blockSignals(False)
            en = c.enabled
            if c.enabled_when:
                other, want = c.enabled_when
                en = en and (self.source.get_control(other) == want)
            w.setEnabled(en)


class DeviceDialog(QtWidgets.QDialog):
    def __init__(self, source: Source, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{source.display_name} settings")
        lay = QtWidgets.QVBoxLayout(self)
        self.form = ControlsForm(source)
        if not source.controls():
            lay.addWidget(QtWidgets.QLabel("This source has no device settings."))
        sc = QtWidgets.QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self.form)
        sc.setFrameShape(QtWidgets.QFrame.NoFrame)
        lay.addWidget(sc)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)
        self.resize(420, min(720, 120 + 34 * len(source.controls())))
