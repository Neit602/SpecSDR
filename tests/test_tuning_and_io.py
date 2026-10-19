import datetime as dt

import numpy as np
import pytest

from pyrx.config.bookmarks import Bookmark, BookmarkStore
from pyrx.config.settings import Settings
from pyrx.core.tuning import TuneContext, plan, snap
from pyrx.core.wavio import WavWriter, from_complex, iq_filename, parse_iq_filename, read_wav_info, to_complex
from pyrx.sources.iqfile import FileMetadataMissing, IqFileSource
from pyrx.ui.freq_display import parse_frequency


def ctx(**kw):
    base = dict(center=100e6, sample_rate=2.4e6, usable_bw=2.0e6, pb_lo=-6e3, pb_hi=6e3)
    base.update(kw)
    return TuneContext(**base)


def test_vfo_inside_span_keeps_center():
    assert plan(100.5e6, ctx()) == (100.5e6, 100e6)


def test_center_follows_when_vfo_leaves_span():
    vfo, c = plan(101.5e6, ctx())
    assert vfo == 101.5e6 and c == 101.5e6


def test_lo_offset_keeps_vfo_off_dc():
    vfo, c = plan(100.0e6 + 1e3, ctx(lo_offset=True, lo_offset_hz=300e3))
    assert c == pytest.approx(vfo - 300e3)
    # passband must not include DC
    assert vfo - 6e3 > c


def test_file_source_clamps_vfo_instead_of_retuning():
    vfo, c = plan(105e6, ctx(tunable=False))
    assert c == 100e6
    assert vfo + 6e3 <= 100e6 + 1.2e6


def test_center_clamped_to_device_range():
    vfo, c = plan(10e3, ctx(freq_min=24e6))
    assert c >= 24e6 and vfo >= 24e6


def test_snap():
    assert snap(100_012_400, 12500) == 100_012_500
    assert snap(7_074_049, 100) == 7_074_000


@pytest.mark.parametrize("text,hz", [("145.5M", 145.5e6), ("7074k", 7.074e6), ("100000000", 1e8),
                                     ("98.1", 98.1e6), ("1.2 GHz", 1.2e9), ("446.00625mhz", 446.00625e6)])
def test_parse_frequency(text, hz):
    assert parse_frequency(text) == pytest.approx(hz)


def test_parse_frequency_rejects_garbage():
    assert parse_frequency("abc") is None


@pytest.mark.parametrize("name,center,rate", [
    ("pyrx_20261019_120000Z_100000000Hz_2400000sps_IQ.wav", 100e6, 2.4e6),
    ("SDRSharp_20200101_120000Z_145500000Hz_IQ.wav", 145.5e6, None),
    ("gqrx_20200101_120000_100000000_2400000_fc.raw", 100e6, 2.4e6),
    ("capture_433.92MHz_2.048Msps.cu8", 433.92e6, 2.048e6),
    ("noinfo.cs8", None, None),
])
def test_filename_metadata(name, center, rate):
    assert parse_iq_filename(name) == (center, rate)


@pytest.mark.parametrize("fmt", ["cu8", "cs16", "cf32"])
def test_wav_roundtrip(tmp_path, fmt):
    rng = np.random.default_rng(0)
    x = ((rng.random(5000) - 0.5) + 1j * (rng.random(5000) - 0.5)).astype(np.complex64)
    p = str(tmp_path / f"t_{fmt}.wav")
    w = WavWriter(p, 2_400_000, 2, fmt, center=100e6)
    w.write_samples(x[:1234])
    w.flush()
    w.write_samples(x[1234:])
    w.close()
    info = read_wav_info(p)
    assert (info.channels, info.sample_rate, info.fmt, info.frames, info.center) == (2, 2400000, fmt, 5000, 100e6)
    raw = np.fromfile(p, dtype={"cu8": np.uint8, "cs16": np.int16, "cf32": np.float32}[fmt],
                      offset=info.data_offset, count=info.frames * 2)
    y = to_complex(raw, fmt)
    tol = {"cu8": 1 / 127, "cs16": 1 / 32767, "cf32": 1e-7}[fmt]
    assert np.max(np.abs(y - x)) <= tol


@pytest.mark.parametrize("fmt,ext", [("cu8", ".cu8"), ("cs8", ".cs8"), ("cf32", ".cf32"), ("cs16", ".cs16")])
def test_raw_file_source(tmp_path, fmt, ext):
    x = (np.exp(2j * np.pi * 0.01 * np.arange(20000)) * 0.5).astype(np.complex64)
    p = tmp_path / f"cap_100MHz_1Msps{ext}"
    from_complex(x, fmt).tofile(p)
    s = IqFileSource()
    s.open(str(p))
    assert s.center_frequency == 100e6 and s.sample_rate == 1e6
    s.set_control("loop", False)
    y, eof = s.read_block(30000)
    assert eof and len(y) == 20000
    assert np.max(np.abs(y - x)) < 0.01


def test_file_source_needs_metadata(tmp_path):
    p = tmp_path / "unknown.cs8"
    np.zeros(100, np.int8).tofile(p)
    s = IqFileSource()
    with pytest.raises(FileMetadataMissing):
        s.open(str(p))
    s.override_center, s.override_rate = 50e6, 1e6
    s.open(str(p))
    assert s.sample_rate == 1e6


def test_file_source_loops(tmp_path):
    x = np.arange(10, dtype=np.float32).astype(np.complex64)
    p = tmp_path / "x_1MHz_1Msps.cf32"
    x.tofile(p)
    s = IqFileSource()
    s.open(str(p))
    y, eof = s.read_block(25)
    assert not eof and np.allclose(y.real, np.concatenate([np.arange(10)] * 3)[:25])


def test_settings_roundtrip(tmp_path):
    s = Settings(str(tmp_path / "s.json"))
    s["radio"]["mode"] = "AM"
    s["display"]["fft_size"] = 16384
    s.save()
    t = Settings(str(tmp_path / "s.json")).load()
    assert t["radio"]["mode"] == "AM" and t["display"]["fft_size"] == 16384
    assert t["agc"]["enabled"] is True  # defaults merged in


def test_bookmarks_roundtrip(tmp_path):
    b = BookmarkStore(str(tmp_path / "b.json"))
    b.add(Bookmark("Airport ATIS", 127.1e6, "AM", 8330, "Air"))
    b.add(Bookmark("PMR 1", 446.00625e6, "NFM", 12500, "PMR"))
    b.save()
    c = BookmarkStore(str(tmp_path / "b.json")).load()
    assert [x.name for x in c.items] == ["Airport ATIS", "PMR 1"]
    assert c.groups() == ["Air", "PMR"]
    c.remove(c.items[0].id)
    assert len(c.items) == 1


def test_iq_filename_has_metadata(tmp_path):
    p = iq_filename(str(tmp_path), 433.92e6, 2.048e6, dt.datetime(2026, 1, 2, 3, 4, 5))
    assert parse_iq_filename(p) == (433.92e6, 2.048e6)
