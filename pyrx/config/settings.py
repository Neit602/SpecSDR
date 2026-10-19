"""Settings persistence (JSON). No Qt: binary Qt state (geometry, splitters) is stored by
the UI as base64 strings."""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile

log = logging.getLogger(__name__)

_config_dir_override: str | None = None


def set_config_dir(path: str | None) -> None:
    global _config_dir_override
    _config_dir_override = path


def config_dir() -> str:
    if _config_dir_override:
        d = _config_dir_override
    elif os.environ.get("PYRX_CONFIG_DIR"):
        d = os.environ["PYRX_CONFIG_DIR"]
    elif sys.platform.startswith("win"):
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PyRX")
    elif sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/PyRX")
    else:
        d = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "pyrx")
    os.makedirs(d, exist_ok=True)
    return d


def default_recordings_dir() -> str:
    home = os.path.expanduser("~")
    for c in ("Music", "Documents"):
        p = os.path.join(home, c)
        if os.path.isdir(p):
            return os.path.join(p, "PyRX")
    return os.path.join(home, "PyRX")


DEFAULTS: dict = {
    "version": 1,
    "source": {"name": "simulated", "device": "", "sample_rate": 2.4e6, "center": 100.0e6,
               "controls": {}, "file": "", "dc_removal": True, "lo_offset": False,
               "lo_offset_hz": 250e3, "swap_iq": False},
    "radio": {"vfo": 99.8e6, "mode": "WFM", "bandwidths": {}, "orders": {}, "filter_window": "blackmanharris",
              "step": 0, "snap": True, "deemphasis_us": 50, "stereo": True, "cw_pitch": 600.0,
              "squelch": False, "squelch_db": -60.0},
    "agc": {"enabled": True, "attack_ms": 5.0, "decay_ms": 250.0, "threshold_db": -90.0,
            "slope_db": 0.0, "hang_ms": 0.0, "manual_gain_db": 0.0},
    "audio": {"device": "default", "volume": 50, "muted": False, "latency_ms": 120},
    "display": {"fft_size": 8192, "window": "blackmanharris", "fps": 30, "averaging": 1, "smoothing": 0.5,
                "peak_hold": False, "ref_db": 0.0, "range_db": 110.0, "wf_speed": 30, "wf_contrast": 0.0,
                "wf_range_db": 70.0, "wf_offset_db": 0.0, "zoom": 1.0, "fill": True,
                "colormap": "classic"},
    "recording": {"directory": "", "iq_format": "cs16"},
    "ui": {"geometry": "", "state": "", "main_splitter": "", "plot_splitter": "", "side_panel": True,
           "sections": {}, "theme": "dark"},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    FILE = "settings.json"

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(config_dir(), self.FILE)
        self.data = copy.deepcopy(DEFAULTS)

    def load(self) -> "Settings":
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data = _merge(DEFAULTS, json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("ignoring unreadable settings %s: %s", self.path, e)
            self.data = copy.deepcopy(DEFAULTS)
        return self

    def save(self) -> None:
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".settings", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)  # atomic: never leaves a half-written file
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def __getitem__(self, section: str) -> dict:
        return self.data.setdefault(section, {})
