"""Minimal WAV reading/writing for IQ and audio (8-bit unsigned, 16-bit, float32),
plus raw IQ formats and filename metadata parsing. No Qt."""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass

import numpy as np

# sample format name -> (numpy dtype of one component, scale to +-1, offset)
FORMATS = {
    "cu8": (np.uint8, 1 / 127.5, 127.5),
    "cs8": (np.int8, 1 / 128.0, 0.0),
    "cs16": (np.int16, 1 / 32768.0, 0.0),
    "cf32": (np.float32, 1.0, 0.0),
}

RAW_EXTENSIONS = {
    ".cu8": "cu8", ".u8": "cu8", ".bin": "cu8",
    ".cs8": "cs8", ".s8": "cs8",
    ".cs16": "cs16", ".s16": "cs16",
    ".cf32": "cf32", ".fc32": "cf32", ".cfile": "cf32", ".raw": "cf32",
}


@dataclass
class WavInfo:
    channels: int
    sample_rate: int
    bits: int
    fmt: str            # 'cu8' / 'cs16' / 'cf32' (per component)
    data_offset: int
    data_bytes: int
    center: float | None = None  # from an 'auxi' chunk, if present

    @property
    def frames(self) -> int:
        return self.data_bytes // (self.channels * self.bits // 8)


def read_wav_info(path: str) -> WavInfo:
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[8:12] != b"WAVE" or head[:4] not in (b"RIFF", b"RF64"):
            raise ValueError("not a WAV file")
        rf64 = head[:4] == b"RF64"
        ds64_data = None
        fmt = None
        center = None
        size = os.path.getsize(path)
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                break
            cid, clen = ch[:4], struct.unpack("<I", ch[4:])[0]
            pos = f.tell()
            if cid == b"ds64":
                body = f.read(clen)
                ds64_data = struct.unpack("<Q", body[8:16])[0]
            elif cid == b"fmt ":
                body = f.read(clen)
                tag, chans, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
                if tag == 0xFFFE and len(body) >= 26:
                    tag = struct.unpack("<H", body[24:26])[0]
                fmt = (tag, chans, rate, bits)
            elif cid == b"auxi":
                body = f.read(clen)
                if len(body) >= 36:
                    c = struct.unpack("<I", body[32:36])[0]
                    center = float(c) if c else None
            elif cid == b"data":
                if fmt is None:
                    raise ValueError("WAV data before fmt chunk")
                if rf64 and clen == 0xFFFFFFFF and ds64_data is not None:
                    clen = ds64_data
                clen = min(clen, size - pos)  # tolerate a header that was never finalised
                tag, chans, rate, bits = fmt
                if tag == 3 and bits == 32:
                    kind = "cf32"
                elif tag == 1 and bits == 16:
                    kind = "cs16"
                elif tag == 1 and bits == 8:
                    kind = "cu8"
                else:
                    raise ValueError(f"unsupported WAV sample format (tag {tag}, {bits} bit)")
                return WavInfo(chans, rate, bits, kind, pos, clen, center)
            f.seek(pos + clen + (clen & 1))
    raise ValueError("WAV file has no data chunk")


def to_complex(raw: np.ndarray, fmt: str) -> np.ndarray:
    """Interleaved I/Q components (any of FORMATS) -> complex64."""
    if fmt == "cf32":
        return np.ascontiguousarray(raw, dtype=np.float32).view(np.complex64)
    _, scale, offset = FORMATS[fmt]
    f = raw.astype(np.float32)
    if offset:
        f -= np.float32(offset)
    f *= np.float32(scale)
    return f.view(np.complex64)


def from_complex(x: np.ndarray, fmt: str) -> np.ndarray:
    """complex64 -> interleaved components in `fmt` (clipped)."""
    f = np.ascontiguousarray(x, dtype=np.complex64).view(np.float32)
    if fmt == "cf32":
        return f
    dt, scale, offset = FORMATS[fmt]
    info = np.iinfo(dt)
    v = np.rint(f / scale + offset)
    return np.clip(v, info.min, info.max).astype(dt)


class WavWriter:
    """Streaming WAV writer. The header is rewritten on flush()/close(), so a file is
    valid (up to the last flush) even if the program dies. Files are split before 4 GiB
    by the caller (see core.recorder)."""

    TAGS = {"cu8": (1, 8), "cs16": (1, 16), "cf32": (3, 32)}

    def __init__(self, path: str, sample_rate: int, channels: int, fmt: str = "cs16",
                 center: float | None = None):
        if fmt not in self.TAGS:
            raise ValueError(f"unsupported WAV format {fmt}")
        self.path = path
        self.sample_rate = int(round(sample_rate))
        self.channels = channels
        self.fmt = fmt
        self.center = center
        self.data_bytes = 0
        self._f = open(path, "wb")
        self._write_header()

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * self.TAGS[self.fmt][1] // 8

    def _header(self) -> bytes:
        tag, bits = self.TAGS[self.fmt]
        block = self.channels * bits // 8
        fmt = struct.pack("<HHIIHH", tag, self.channels, self.sample_rate, self.sample_rate * block, block, bits)
        chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
        if self.center is not None and 0 < self.center < 2 ** 32:
            # 'auxi' chunk as used by several SDR programs: two SYSTEMTIMEs, then centre Hz
            auxi = bytes(32) + struct.pack("<I", int(round(self.center))) + struct.pack("<I", self.sample_rate) + bytes(28)
            chunks += b"auxi" + struct.pack("<I", len(auxi)) + auxi
        riff_len = 4 + len(chunks) + 8 + self.data_bytes
        return b"RIFF" + struct.pack("<I", min(riff_len, 0xFFFFFFFF)) + b"WAVE" + chunks + b"data" + \
            struct.pack("<I", min(self.data_bytes, 0xFFFFFFFF))

    def _write_header(self) -> None:
        h = self._header()
        self._header_len = len(h)
        self._f.seek(0)
        self._f.write(h)
        self._f.seek(self._header_len + self.data_bytes)

    def write_samples(self, data: np.ndarray) -> int:
        """data: interleaved components already in the file's dtype, or float audio / complex IQ."""
        if np.iscomplexobj(data):
            raw = from_complex(data, self.fmt)
        elif self.fmt == "cf32":
            raw = np.ascontiguousarray(data, dtype=np.float32)
        elif data.dtype.kind == "f":
            dt, scale, offset = FORMATS[self.fmt]
            info = np.iinfo(dt)
            raw = np.clip(np.rint(np.asarray(data) / scale + offset), info.min, info.max).astype(dt)
        else:
            raw = data
        b = raw.tobytes()
        self._f.write(b)
        self.data_bytes += len(b)
        return len(b)

    def flush(self) -> None:
        self._f.flush()
        self._write_header()

    def close(self) -> None:
        if not self._f.closed:
            self._write_header()
            self._f.close()


# ------------------------------------------------------------------ filename metadata

_UNIT = {"": 1.0, "k": 1e3, "m": 1e6, "g": 1e9}


def parse_iq_filename(path: str) -> tuple[float | None, float | None]:
    """(centre Hz, sample rate) from common IQ recording filename conventions:
    'pyrx_..._100000000Hz_2400000sps_IQ.wav', 'SDRSharp_..._100000000Hz_IQ.wav',
    'gqrx_20200101_120000_100000000_2400000_fc.raw', '..._100.1MHz_2.4Msps.cu8'."""
    name = os.path.basename(path)
    m = re.search(r"gqrx_\d{8}_\d{6}_(\d+)_(\d+)_fc", name)
    if m:
        return float(m.group(1)), float(m.group(2))
    center = rate = None
    m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*([kKmMgG]?)(?:sps|SPS|Sps|S/s|s/s|SpS)(?![a-zA-Z])", name)
    if m:
        rate = float(m.group(1)) * _UNIT[m.group(2).lower()]
    for m in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*([kKmMgG]?)[hH][zZ](?![a-zA-Z])", name):
        center = float(m.group(1)) * _UNIT[m.group(2).lower()]
        break
    return center, rate


def iq_filename(directory: str, center: float, rate: float, when, ext: str = ".wav", tag: str = "IQ") -> str:
    stamp = when.strftime("%Y%m%d_%H%M%SZ")
    return os.path.join(directory, f"pyrx_{stamp}_{int(round(center))}Hz_{int(round(rate))}sps_{tag}{ext}")
