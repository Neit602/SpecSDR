"""Common device abstraction.

A Source produces complex64 IQ blocks on its own thread and hands them to a callback:

    callback(samples: np.ndarray[complex64], center_hz: float, sample_rate: float)

Device-specific settings are described declaratively by `controls()` (a list of Control),
so the UI can build a settings panel for any device without knowing about it."""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

log = logging.getLogger(__name__)

BlockCallback = Callable[[np.ndarray, float, float], None]
ErrorCallback = Callable[[str, str], None]  # (message, kind) kind: "error" | "eof"


class SourceError(Exception):
    """A device could not be opened / configured / read. The message is user-facing."""


@dataclass
class DeviceInfo:
    id: str          # what open() takes
    label: str       # shown to the user
    serial: str = ""


@dataclass
class Control:
    """One user-adjustable device setting.

    kind: 'bool' (checkbox), 'choice' (combo; choices = [(value, label)]),
          'slider' (int/float with minimum/maximum/step), 'number' (spin box),
          'info' (read-only text).
    confirm: if set, enabling a bool (or changing the value) asks this question first.
    enabled_when: (other_key, value) - only enabled while that control has that value.
    """

    key: str
    label: str
    kind: str
    default: Any = None
    choices: list | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str = ""
    decimals: int = 0
    tooltip: str = ""
    confirm: str | None = None
    enabled: bool = True
    enabled_when: tuple | None = None
    group: str = ""


class Source(ABC):
    """Base class. Subclasses implement the _hw_* hooks and their own reader thread."""

    name: str = "base"            # registry key, used on the command line
    display_name: str = "Base"
    tunable: bool = True          # False for file playback: centre frequency is fixed

    def __init__(self):
        self._lock = threading.RLock()
        self._center = 100e6
        self._rate = 2.4e6
        self._running = False
        self._open = False
        self._callback: BlockCallback | None = None
        self._error_cb: ErrorCallback | None = None
        self._values: dict[str, Any] = {}
        self.overflows = 0        # blocks lost inside the source / driver
        for c in self.controls():
            if c.kind != "info":
                self._values[c.key] = c.default

    # --------------------------------------------------------------- discovery
    @classmethod
    def available(cls) -> tuple[bool, str]:
        """(usable, reason). False when a driver library is missing."""
        return True, ""

    @classmethod
    def enumerate(cls) -> list[DeviceInfo]:
        return []

    # --------------------------------------------------------------- lifecycle
    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def is_running(self) -> bool:
        return self._running

    @abstractmethod
    def open(self, device_id: str | None = None) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...

    @abstractmethod
    def start(self, callback: BlockCallback, error_callback: ErrorCallback | None = None) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    def _report_error(self, message: str, kind: str = "error") -> None:
        """Called from the reader thread when streaming ends on its own (fatal error, or
        end of file); the engine then stops the stream and tells the user once."""
        if kind == "error":
            log.error("%s: %s", self.display_name, message)
        else:
            log.info("%s: %s", self.display_name, message)
        cb = self._error_cb
        if cb:
            cb(message, kind)

    # --------------------------------------------------------------- tuning
    @property
    def center_frequency(self) -> float:
        return self._center

    def set_center_frequency(self, hz: float) -> float:
        lo, hi = self.frequency_range()
        hz = float(min(max(hz, lo), hi))
        with self._lock:
            self._center = self._hw_set_center(hz)
        return self._center

    def _hw_set_center(self, hz: float) -> float:
        return hz

    def frequency_range(self) -> tuple[float, float]:
        return 0.0, 6e9

    @property
    def sample_rate(self) -> float:
        return self._rate

    def set_sample_rate(self, hz: float) -> float:
        hz = float(hz)
        rng = self.sample_rate_range()
        rates = self.sample_rates()
        if rng is not None:
            if not (rng[0] <= hz <= rng[1]):
                raise SourceError(f"Sample rate {hz / 1e6:g} MS/s outside {rng[0] / 1e6:g}-{rng[1] / 1e6:g} MS/s")
        elif rates and hz not in rates:
            hz = min(rates, key=lambda r: abs(r - hz))
        with self._lock:
            self._rate = self._hw_set_rate(hz)
        return self._rate

    def _hw_set_rate(self, hz: float) -> float:
        return hz

    def sample_rates(self) -> list[float]:
        """Preset sample rates offered in the UI."""
        return [self._rate]

    def sample_rate_range(self) -> tuple[float, float] | None:
        """If not None, any rate in this range is accepted (presets are just suggestions)."""
        return None

    def usable_bandwidth(self) -> float:
        """Part of the sample rate that is free of anti-alias roll-off."""
        return self._rate * 0.9

    # --------------------------------------------------------------- settings
    def controls(self) -> list[Control]:
        return []

    def get_control(self, key: str) -> Any:
        return self._values.get(key)

    def set_control(self, key: str, value: Any) -> None:
        with self._lock:
            self._apply_control(key, value)
            self._values[key] = value

    def _apply_control(self, key: str, value: Any) -> None:
        pass

    def control_state(self) -> dict[str, Any]:
        return dict(self._values)

    def restore_controls(self, values: dict[str, Any]) -> None:
        # settings that need a confirmation (antenna power, bias-T) are never restored on
        keys = {c.key for c in self.controls() if c.kind != "info" and not c.confirm}
        for k, v in (values or {}).items():
            if k in keys:
                try:
                    self.set_control(k, v)
                except Exception as e:  # stale / unsupported value in old settings
                    log.warning("could not restore %s=%r: %s", k, v, e)

    def info_text(self) -> str:
        return ""
