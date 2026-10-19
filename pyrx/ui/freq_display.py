"""Large frequency display.

- mouse wheel over a digit: +/- one unit of that digit
- click the upper / lower half of a digit: +/- one unit
- keyboard (when focused): 0-9 overwrite the digit under the cursor and move right,
  Left/Right move the cursor, Up/Down change the digit, Enter (or F2 / double-click on the
  label area) opens a direct-entry box accepting e.g. "145.5M", "7074k", "100000000"."""

from __future__ import annotations

import re

from PyQt5 import QtCore, QtGui, QtWidgets

from . import theme


def parse_frequency(text: str, default_unit: float = 1e6) -> float | None:
    """'145.5M' -> 145.5e6, '7074k' -> 7.074e6, '100000000' -> 1e8, '98.1' -> 98.1 MHz
    (plain numbers below 100 000 are taken in `default_unit`)."""
    t = text.strip().replace(" ", "").replace(",", ".").lower()
    if not t:
        return None
    m = re.fullmatch(r"([0-9]*\.?[0-9]+(?:e[+-]?\d+)?)\s*(hz|khz|mhz|ghz|k|m|g)?", t)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2)
    mult = {"hz": 1, "k": 1e3, "khz": 1e3, "m": 1e6, "mhz": 1e6, "g": 1e9, "ghz": 1e9}.get(unit or "", None)
    if mult is None:
        mult = 1.0 if v >= 100000 else default_unit
    return v * mult


class FrequencyDisplay(QtWidgets.QWidget):
    frequencyChanged = QtCore.pyqtSignal(float)  # user changes only

    def __init__(self, digits: int = 10, digit_px: int = 34, parent=None, title: str = ""):
        super().__init__(parent)
        self.digits = digits
        self.title = title
        self._hz = 0
        self._lo, self._hi = 0.0, 10 ** digits - 1
        self._hover = -1
        self._cursor = 6  # digit index (power of ten) the keyboard edits
        self._wheel_acc = 0
        self.font_ = QtGui.QFont("DejaVu Sans Mono")
        self.font_.setStyleHint(QtGui.QFont.Monospace)
        self.font_.setPixelSize(digit_px)
        self.font_.setBold(True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setToolTip("Wheel or click the upper/lower half of a digit to change it.\n"
                        "Type digits to overwrite, arrows to move/change, Enter for direct entry.")
        fm = QtGui.QFontMetrics(self.font_)
        self._dw = fm.horizontalAdvance("0") + 2
        self._sep = max(6, self._dw // 3)
        self._h = fm.height() + 6
        self.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)

    # ------------------------------------------------------------------ api
    def frequency(self) -> float:
        return float(self._hz)

    def setFrequency(self, hz: float) -> None:
        hz = int(round(min(max(hz, self._lo), self._hi)))
        if hz != self._hz:
            self._hz = hz
            self.update()

    def setRange(self, lo: float, hi: float) -> None:
        self._lo, self._hi = float(lo), float(min(hi, 10 ** self.digits - 1))

    def _user_set(self, hz: float) -> None:
        hz = int(round(min(max(hz, self._lo), self._hi)))
        if hz != self._hz:
            self._hz = hz
            self.update()
            self.frequencyChanged.emit(float(hz))

    # ------------------------------------------------------------------ geometry
    def sizeHint(self):
        groups = (self.digits - 1) // 3
        return QtCore.QSize(self.digits * self._dw + groups * self._sep + 8, self._h)

    minimumSizeHint = sizeHint

    def _digit_rects(self) -> list[tuple[int, QtCore.QRect]]:
        """[(power_of_ten, rect)] from the most significant digit."""
        out = []
        x = 4
        for i in range(self.digits):
            p = self.digits - 1 - i
            out.append((p, QtCore.QRect(x, 0, self._dw, self.height())))
            x += self._dw
            if p % 3 == 0 and p:
                x += self._sep
        return out

    def _digit_at(self, pos: QtCore.QPoint) -> int:
        for p, r in self._digit_rects():
            if r.adjusted(-2, 0, 2, 0).contains(pos):
                return p
        return -1

    # ------------------------------------------------------------------ painting
    def paintEvent(self, _ev):
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.TextAntialiasing)
        qp.fillRect(self.rect(), QtGui.QColor("#101215"))
        qp.setFont(self.font_)
        s = str(self._hz).rjust(self.digits, "0")
        first_sig = next((i for i, ch in enumerate(s) if ch != "0"), self.digits - 1)
        for i, (p, r) in enumerate(self._digit_rects()):
            if p == self._hover:
                qp.fillRect(r, QtGui.QColor(255, 255, 255, 28))
                half = QtCore.QRect(r.x(), r.y(), r.width(), r.height() // 2)
                qp.fillRect(half, QtGui.QColor(255, 255, 255, 12))
            dim = i < first_sig
            qp.setPen(QtGui.QColor(theme.FREQ_DIGITS_DIM if dim else theme.FREQ_DIGITS))
            qp.drawText(r, QtCore.Qt.AlignCenter, s[i])
            if self.hasFocus() and p == self._cursor:
                qp.fillRect(QtCore.QRect(r.x() + 2, r.bottom() - 2, r.width() - 4, 2), QtGui.QColor(theme.ACCENT))
            if p % 3 == 0 and p:
                qp.setPen(QtGui.QColor(theme.TEXT_DIM))
                qp.drawText(QtCore.QRect(r.right(), r.y(), self._sep, r.height()), QtCore.Qt.AlignCenter, ".")

    # ------------------------------------------------------------------ mouse
    def mouseMoveEvent(self, ev):
        h = self._digit_at(ev.pos())
        if h != self._hover:
            self._hover = h
            self.update()

    def leaveEvent(self, _ev):
        self._hover = -1
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() != QtCore.Qt.LeftButton:
            return
        p = self._digit_at(ev.pos())
        if p < 0:
            return
        self._cursor = p
        up = ev.pos().y() < self.height() / 2
        self._user_set(self._hz + (10 ** p if up else -(10 ** p)))
        self.setFocus()

    def wheelEvent(self, ev):
        p = self._digit_at(ev.pos())
        if p < 0:
            ev.ignore()
            return
        self._wheel_acc += ev.angleDelta().y()
        steps = int(self._wheel_acc / 120)
        if steps:
            self._wheel_acc -= steps * 120
            self._user_set(self._hz + steps * 10 ** p)
        ev.accept()

    # ------------------------------------------------------------------ keyboard
    _KEYS = {QtCore.Qt.Key_Left, QtCore.Qt.Key_Right, QtCore.Qt.Key_Up, QtCore.Qt.Key_Down,
             QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_F2, QtCore.Qt.Key_Escape}

    def event(self, ev):
        # claim keys before window-level shortcuts (arrows = step tune, Space = play) see them
        if ev.type() == QtCore.QEvent.ShortcutOverride:
            if ev.key() in self._KEYS or QtCore.Qt.Key_0 <= ev.key() <= QtCore.Qt.Key_9:
                ev.accept()
                return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        k = ev.key()
        if QtCore.Qt.Key_0 <= k <= QtCore.Qt.Key_9:
            d = k - QtCore.Qt.Key_0
            p = self._cursor
            cur = (self._hz // 10 ** p) % 10
            self._user_set(self._hz + (d - cur) * 10 ** p)
            self._cursor = max(0, p - 1)
            self.update()
        elif k == QtCore.Qt.Key_Left:
            self._cursor = min(self.digits - 1, self._cursor + 1)
            self.update()
        elif k == QtCore.Qt.Key_Right:
            self._cursor = max(0, self._cursor - 1)
            self.update()
        elif k == QtCore.Qt.Key_Up:
            self._user_set(self._hz + 10 ** self._cursor)
        elif k == QtCore.Qt.Key_Down:
            self._user_set(self._hz - 10 ** self._cursor)
        elif k in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter, QtCore.Qt.Key_F2):
            self.directEntry()
        elif k == QtCore.Qt.Key_Escape:
            self.clearFocus()
        else:
            super().keyPressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        # a double click on a digit is two clicks (+1 +1); only the gaps open the entry box
        if self._digit_at(ev.pos()) < 0:
            self.directEntry()

    def directEntry(self) -> None:
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Enter frequency", "Frequency (e.g. 145.5M, 7074k, 100000000 Hz; plain numbers < 100000 are MHz):",
            text=f"{self._hz / 1e6:.6f}M")
        if ok:
            f = parse_frequency(text)
            if f is None:
                QtWidgets.QMessageBox.warning(self, "Frequency", f"Cannot understand '{text}'.")
            else:
                self._user_set(f)

    def focusInEvent(self, ev):
        self.update()
        super().focusInEvent(ev)

    def focusOutEvent(self, ev):
        self.update()
        super().focusOutEvent(ev)
