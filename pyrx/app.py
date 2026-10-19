"""Application entry point: command line, logging, exception hooks, Qt setup."""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time

log = logging.getLogger("pyrx")


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="python -m pyrx", description="PyRX - receive-only SDR")
    p.add_argument("--source", choices=["simulated", "file", "rtlsdr", "hackrf"], help="signal source to select")
    p.add_argument("--file", help="IQ file to play (implies --source file)")
    p.add_argument("--freq", type=float, help="VFO frequency in Hz")
    p.add_argument("--mode", help="demodulator (NFM, AM, LSB, USB, WFM, DSB, CW, RAW)")
    p.add_argument("--autostart", action="store_true", help="start streaming immediately")
    p.add_argument("--exit-after", type=float, metavar="SECONDS", help="quit after this many seconds")
    p.add_argument("--no-audio", action="store_true", help="use the silent null audio sink")
    p.add_argument("--config-dir", help="settings / bookmarks directory (default: per-user config dir)")
    p.add_argument("--reset-settings", action="store_true", help="ignore saved settings")
    p.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING, ERROR")
    p.add_argument("--stats", action="store_true", help="log performance statistics every second")
    a = p.parse_args(argv)
    if a.file and not a.source:
        a.source = "file"
    return a


def _install_hooks():
    def excepthook(t, v, tb):
        log.critical("Unhandled exception", exc_info=(t, v, tb))

    def thread_hook(args):
        log.critical("Unhandled exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = excepthook
    threading.excepthook = thread_hook


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    _install_hooks()

    from .config.settings import Settings, set_config_dir
    if args.config_dir:
        set_config_dir(args.config_dir)
    settings = Settings()
    if not args.reset_settings:
        settings.load()

    from PyQt5 import QtCore, QtWidgets
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("PyRX")
    from .ui import theme
    theme.apply_dark(app)
    from .ui.main_window import MainWindow
    win = MainWindow(settings, args)
    win.show()

    if args.autostart:
        QtCore.QTimer.singleShot(200, win.start)
    t_start = time.monotonic()
    if args.stats:
        # GUI responsiveness probe: how late does a 50 ms timer fire (worst case per second)?
        lag = {"last": time.monotonic(), "max": 0.0}

        def probe():
            now = time.monotonic()
            lag["max"] = max(lag["max"], now - lag["last"] - 0.05)
            lag["last"] = now
        ptimer = QtCore.QTimer()
        ptimer.setTimerType(QtCore.Qt.PreciseTimer)
        ptimer.timeout.connect(probe)
        ptimer.start(50)

        def stats():
            st = win.engine.stats()
            log.info("gui: worst timer lag %.0f ms", lag["max"] * 1000)
            lag["max"] = 0.0
            log.info("stats: rate=%.3f MS/s drops=%d+%d fps=%.1f cpu=%.0f%% dsp=%.0f%% audio fill=%.0fms "
                     "U%d(dev %d)/O%d drift=%+.0fppm level=%.1f snr=%.1f", st.effective_rate / 1e6, st.dropped_blocks,
                     st.source_overflows, win.spectrum.fps, win.cpu, st.dsp_load * 100, st.audio_fill_ms,
                     st.audio_underruns, st.device_underflows, st.audio_overruns, st.drift_ppm, st.level_db,
                     st.snr_db)
        timer = QtCore.QTimer()
        timer.timeout.connect(stats)
        timer.start(1000)
    if args.exit_after:
        QtCore.QTimer.singleShot(int(args.exit_after * 1000), win.close)
    rc = app.exec_()
    st = win.engine.st
    log.info("exit: ran %.1f s, %d blocks, %d dropped, %d source overflows, %d DSP errors, %d audio underruns",
             time.monotonic() - t_start, st.blocks, st.dropped_blocks, win.engine.source.overflows if
             win.engine.source else 0, st.dsp_errors, win.engine.sink.underruns)
    return rc
