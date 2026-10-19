"""Thin ctypes binding to libhackrf - receive functions only.

Deliberately RX-only: no transmit function (hackrf_start_tx, hackrf_set_txvga_gain, ...)
is bound, so PyRX cannot transmit even by accident. Signatures follow libhackrf's
hackrf.h (checked against release 2026.01.3; the API used here has been stable since 2014)."""

from __future__ import annotations

import atexit
import ctypes
import ctypes.util
import sys
from ctypes import (CFUNCTYPE, POINTER, Structure, byref, c_char_p, c_double, c_int, c_uint8,
                    c_uint32, c_uint64, c_void_p)

HACKRF_SUCCESS = 0
HACKRF_TRUE = 1


class hackrf_transfer(Structure):
    _fields_ = [
        ("device", c_void_p),
        ("buffer", POINTER(c_uint8)),
        ("buffer_length", c_int),
        ("valid_length", c_int),
        ("rx_ctx", c_void_p),
        ("tx_ctx", c_void_p),
    ]


class hackrf_device_list_t(Structure):
    _fields_ = [
        ("serial_numbers", POINTER(c_char_p)),
        ("usb_board_ids", POINTER(c_int)),
        ("usb_device_index", POINTER(c_int)),
        ("devicecount", c_int),
        ("usb_devices", POINTER(c_void_p)),
        ("usb_devicecount", c_int),
    ]


sample_block_cb_fn = CFUNCTYPE(c_int, POINTER(hackrf_transfer))

_lib = None
_error = ""
_initialised = False


def _candidates():
    if sys.platform.startswith("win"):
        return ["hackrf.dll", "libhackrf.dll", ctypes.util.find_library("hackrf")]
    if sys.platform == "darwin":
        return ["libhackrf.dylib", "libhackrf.0.dylib", "/opt/homebrew/lib/libhackrf.dylib",
                "/usr/local/lib/libhackrf.dylib", ctypes.util.find_library("hackrf")]
    return ["libhackrf.so.0", "libhackrf.so", ctypes.util.find_library("hackrf")]


def _declare(lib):
    def d(name, res, args):
        f = getattr(lib, name)
        f.restype, f.argtypes = res, args

    dev = c_void_p
    d("hackrf_init", c_int, [])
    d("hackrf_exit", c_int, [])
    d("hackrf_library_version", c_char_p, [])
    d("hackrf_library_release", c_char_p, [])
    d("hackrf_device_list", POINTER(hackrf_device_list_t), [])
    d("hackrf_device_list_free", None, [POINTER(hackrf_device_list_t)])
    d("hackrf_open_by_serial", c_int, [c_char_p, POINTER(dev)])
    d("hackrf_close", c_int, [dev])
    d("hackrf_start_rx", c_int, [dev, sample_block_cb_fn, c_void_p])
    d("hackrf_stop_rx", c_int, [dev])
    d("hackrf_is_streaming", c_int, [dev])
    d("hackrf_set_freq", c_int, [dev, c_uint64])
    d("hackrf_set_sample_rate", c_int, [dev, c_double])
    d("hackrf_set_baseband_filter_bandwidth", c_int, [dev, c_uint32])
    d("hackrf_compute_baseband_filter_bw", c_uint32, [c_uint32])
    d("hackrf_set_lna_gain", c_int, [dev, c_uint32])
    d("hackrf_set_vga_gain", c_int, [dev, c_uint32])
    d("hackrf_set_amp_enable", c_int, [dev, c_uint8])
    d("hackrf_set_antenna_enable", c_int, [dev, c_uint8])
    d("hackrf_error_name", c_char_p, [c_int])
    d("hackrf_board_id_read", c_int, [dev, POINTER(c_uint8)])
    d("hackrf_board_id_name", c_char_p, [c_int])
    d("hackrf_version_string_read", c_int, [dev, c_char_p, c_uint8])


def load():
    """Load and initialise libhackrf once. Returns the CDLL or None (see error())."""
    global _lib, _error, _initialised
    if _lib is not None or _error:
        return _lib
    from ._dlls import add_windows_dll_dirs
    add_windows_dll_dirs()
    last = "not found"
    for name in _candidates():
        if not name:
            continue
        try:
            lib = ctypes.CDLL(name)
            _declare(lib)
            break
        except (OSError, AttributeError) as e:
            last = str(e)
    else:
        _error = f"libhackrf could not be loaded ({last})"
        return None
    r = lib.hackrf_init()
    if r != HACKRF_SUCCESS:
        _error = f"hackrf_init failed: {lib.hackrf_error_name(r).decode()}"
        return None
    _initialised = True
    _lib = lib
    atexit.register(_shutdown)
    return _lib


def _shutdown():
    if _lib is not None and _initialised:
        try:
            _lib.hackrf_exit()
        except Exception:
            pass


def error() -> str:
    return _error


def error_name(code: int) -> str:
    if _lib is None:
        return str(code)
    try:
        return _lib.hackrf_error_name(code).decode(errors="replace")
    except Exception:
        return str(code)


def list_serials() -> list[str]:
    lib = load()
    if lib is None:
        return []
    lst = lib.hackrf_device_list()
    if not lst:
        return []
    try:
        out = []
        for i in range(lst.contents.devicecount):
            s = lst.contents.serial_numbers[i]
            out.append(s.decode() if s else "")
        return out
    finally:
        lib.hackrf_device_list_free(lst)


def open_device(serial: str | None) -> c_void_p:
    lib = load()
    devp = c_void_p()
    r = lib.hackrf_open_by_serial(serial.encode() if serial else None, byref(devp))
    if r != HACKRF_SUCCESS:
        raise OSError(r, error_name(r))
    return devp
