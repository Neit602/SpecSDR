"""Dark theme (Fusion + palette + a little stylesheet) and shared colours."""

from __future__ import annotations

from PyQt5 import QtGui, QtWidgets

BG = "#1b1d21"
BG_ALT = "#23262b"
PANEL = "#2a2d33"
TEXT = "#d8dadf"
TEXT_DIM = "#8a8f98"
ACCENT = "#3d8ee6"
VFO = "#ff4040"
PASSBAND = (61, 142, 230, 60)
SPECTRUM_LINE = "#e8e8e8"
SPECTRUM_FILL_TOP = (61, 142, 230, 150)
SPECTRUM_FILL_BOTTOM = (61, 142, 230, 15)
PEAK_LINE = "#e0b040"
GRID = (255, 255, 255, 30)
FREQ_DIGITS = "#f2f2f2"
FREQ_DIGITS_DIM = "#4a4f57"

STYLESHEET = f"""
QWidget {{ font-size: 9pt; }}
QToolTip {{ color: {TEXT}; background: {PANEL}; border: 1px solid #444; }}
QGroupBox {{ border: 1px solid #3a3e45; border-radius: 3px; margin-top: 8px; padding-top: 4px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 6px; padding: 0 3px; color: {TEXT_DIM}; }}
QPushButton#sectionHeader {{ text-align: left; padding: 4px 6px; border: none; font-weight: bold;
    background: {PANEL}; color: {TEXT}; }}
QPushButton#sectionHeader:hover {{ background: #33373e; }}
QStatusBar QLabel {{ padding: 0 6px; color: {TEXT}; }}
QSplitter::handle {{ background: #33373e; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: #3a3e45; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 12px; margin: -5px 0; border-radius: 6px; }}
QSlider::groove:vertical {{ width: 4px; background: #3a3e45; border-radius: 2px; }}
QSlider::handle:vertical {{ background: {ACCENT}; height: 12px; margin: 0 -5px; border-radius: 6px; }}
QToolButton:checked {{ background: #2f5f99; }}
"""


def apply_dark(app: QtWidgets.QApplication) -> None:
    app.setStyle("Fusion")
    p = QtGui.QPalette()
    c = QtGui.QColor
    p.setColor(QtGui.QPalette.Window, c(BG))
    p.setColor(QtGui.QPalette.WindowText, c(TEXT))
    p.setColor(QtGui.QPalette.Base, c("#15171a"))
    p.setColor(QtGui.QPalette.AlternateBase, c(BG_ALT))
    p.setColor(QtGui.QPalette.ToolTipBase, c(PANEL))
    p.setColor(QtGui.QPalette.ToolTipText, c(TEXT))
    p.setColor(QtGui.QPalette.Text, c(TEXT))
    p.setColor(QtGui.QPalette.Button, c(PANEL))
    p.setColor(QtGui.QPalette.ButtonText, c(TEXT))
    p.setColor(QtGui.QPalette.BrightText, c("#ff5555"))
    p.setColor(QtGui.QPalette.Highlight, c(ACCENT))
    p.setColor(QtGui.QPalette.HighlightedText, c("#ffffff"))
    p.setColor(QtGui.QPalette.Link, c(ACCENT))
    for role in (QtGui.QPalette.Text, QtGui.QPalette.ButtonText, QtGui.QPalette.WindowText):
        p.setColor(QtGui.QPalette.Disabled, role, c("#62666e"))
    app.setPalette(p)
    app.setStyleSheet(STYLESHEET)


# waterfall colour maps: list of (position 0..1, (r, g, b))
COLORMAPS = {
    "classic": [(0.0, (0, 0, 0)), (0.15, (0, 0, 90)), (0.35, (0, 70, 255)), (0.55, (0, 220, 230)),
                (0.72, (250, 250, 0)), (0.88, (255, 110, 0)), (1.0, (255, 0, 0))],
    "heat": [(0.0, (0, 0, 0)), (0.3, (120, 0, 0)), (0.6, (255, 120, 0)), (0.85, (255, 230, 60)), (1.0, (255, 255, 255))],
    "gray": [(0.0, (0, 0, 0)), (1.0, (255, 255, 255))],
    "ocean": [(0.0, (2, 4, 20)), (0.35, (20, 40, 120)), (0.65, (40, 170, 190)), (1.0, (230, 255, 240))],
}


def colormap_lut(name: str, n: int = 256):
    """uint32 ARGB lookup table."""
    import numpy as np
    stops = COLORMAPS.get(name, COLORMAPS["classic"])
    pos = np.array([s[0] for s in stops])
    cols = np.array([s[1] for s in stops], dtype=np.float64)
    x = np.linspace(0, 1, n)
    r = np.interp(x, pos, cols[:, 0])
    g = np.interp(x, pos, cols[:, 1])
    b = np.interp(x, pos, cols[:, 2])
    return (0xFF000000 | (r.astype(np.uint32) << 16) | (g.astype(np.uint32) << 8) | b.astype(np.uint32)).astype(np.uint32)
