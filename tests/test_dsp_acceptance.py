"""Acceptance tests 1-7 from TASK.md (DSP level, no hardware, no audio device)."""

import math

import numpy as np
import pytest

from pyrx.dsp.agc import Agc
from pyrx.dsp.filters import MultiStageDecimator, choose_decimation
from pyrx.dsp.receiver import AgcSettings, ReceiverChain
from pyrx.dsp.spectrum import SpectrumAnalyzer
from pyrx.dsp.synth import am_modulate, awgn, complex_tone, fm_modulate, tone

from helpers import dominant, feed, level_at

FS = 2.4e6
AUDIO = 48000.0
SETTLE = int(0.3 * AUDIO)


def no_agc():
    return AgcSettings(enabled=False)


def test_nfm_1khz_at_plus_100k():
    n = int(FS * 1.5)
    audio = tone(1000, FS, n)
    iq = fm_modulate(audio, FS, 2500.0, offset=100e3) * np.float32(0.5) + awgn(n, -60)
    chain = ReceiverChain(FS, AUDIO, "NFM", offset=100e3, agc=no_agc())
    out = feed(chain, iq)[SETTLE:]
    f, pk, other = dominant(out, AUDIO)
    assert abs(f - 1000) <= 5, f
    assert pk - other >= 20, (pk, other)


def test_am_1khz_80pct():
    n = int(FS * 1.5)
    audio = tone(1000, FS, n)
    iq = am_modulate(audio, FS, 0.8, offset=100e3, carrier=0.3) + awgn(n, -60)
    chain = ReceiverChain(FS, AUDIO, "AM", offset=100e3, agc=no_agc())
    out = feed(chain, iq)[SETTLE:]
    f, pk, other = dominant(out, AUDIO)
    assert abs(f - 1000) <= 5, f
    assert pk - other >= 20, (pk, other)


@pytest.mark.parametrize("agc_on", [False, True])
def test_ssb_usb_and_lsb_rejection(agc_on):
    n = int(FS * 1.5)
    vfo = 50e3
    iq = complex_tone(vfo + 1500, FS, n, 0.1) + awgn(n, -70)
    usb = feed(ReceiverChain(FS, AUDIO, "USB", offset=vfo, agc=no_agc()), iq)[SETTLE:]
    lsb = feed(ReceiverChain(FS, AUDIO, "LSB", offset=vfo, agc=no_agc()), iq)[SETTLE:]
    f, pk, other = dominant(usb, AUDIO)
    assert abs(f - 1500) <= 5, f
    assert pk - other >= 20
    rejection = level_at(usb, AUDIO, 1500) - level_at(lsb, AUDIO, 1500)
    assert rejection >= 30, rejection
    if agc_on:
        # with AGC the USB tone is normalised; LSB residue must still be far below it
        a = AgcSettings(enabled=True)
        usb_a = feed(ReceiverChain(FS, AUDIO, "USB", offset=vfo, agc=a), iq)[SETTLE:]
        assert abs(dominant(usb_a, AUDIO)[0] - 1500) <= 5


@pytest.mark.parametrize("tau", [50e-6, 75e-6])
def test_wfm_deemphasis(tau):
    n = int(FS * 1.5)
    audio = tone(1000, FS, n, 0.4) + tone(10000, FS, n, 0.4)
    iq = fm_modulate(audio, FS, 75e3, offset=-300e3) * np.float32(0.5) + awgn(n, -50)
    chain = ReceiverChain(FS, AUDIO, "WFM", offset=-300e3, deemphasis_tau=tau, agc=no_agc())
    out = feed(chain, iq)[SETTLE:]
    f, pk, other = dominant(out, AUDIO)
    assert abs(f - 1000) <= 5
    measured = level_at(out, AUDIO, 10000) - level_at(out, AUDIO, 1000)
    expected = 10 * math.log10((1 + (2 * math.pi * 1000 * tau) ** 2) / (1 + (2 * math.pi * 10000 * tau) ** 2))
    assert abs(measured - expected) < 1.0, (measured, expected)


def test_wfm_no_deemphasis_is_flat():
    n = int(FS * 1.0)
    audio = tone(1000, FS, n, 0.4) + tone(10000, FS, n, 0.4)
    iq = fm_modulate(audio, FS, 75e3, offset=200e3)
    out = feed(ReceiverChain(FS, AUDIO, "WFM", offset=200e3, deemphasis_tau=0, agc=no_agc()), iq)[SETTLE:]
    assert abs(level_at(out, AUDIO, 10000) - level_at(out, AUDIO, 1000)) < 0.5


@pytest.mark.parametrize("mode", ["NFM", "AM", "USB", "LSB", "CW", "DSB", "WFM", "RAW"])
def test_block_continuity(mode):
    rng = np.random.default_rng(42)
    n = int(FS * 0.5)
    audio = tone(700, FS, n) * 0.5 + tone(1900, FS, n) * 0.3
    iq = (fm_modulate(audio, FS, 3000, offset=120e3) * np.float32(0.3)
          + am_modulate(audio, FS, 0.5, offset=-80e3, carrier=0.2) + awgn(n, -40, rng))

    def make():
        c = ReceiverChain(FS, AUDIO, mode, offset=120e3 if mode in ("NFM", "WFM") else -80e3,
                          squelch_enabled=True, squelch_db=-50, agc=AgcSettings(enabled=True),
                          stereo=True)
        return c

    whole = make().process(iq)
    c = make()
    parts, i = [], 0
    while i < n:
        k = int(rng.integers(1, 40000))
        parts.append(c.process(iq[i:i + k]))
        i += k
    split = np.concatenate(parts)
    assert split.shape == whole.shape
    assert np.max(np.abs(split - whole)) < 1e-4


@pytest.mark.parametrize("fs", [2.4e6, 10e6])
def test_decimation_rejects_aliasing_tone(fs):
    d = choose_decimation(fs, 48e3)
    fs_out = fs / d
    dec = MultiStageDecimator(fs, d, 0.42 * fs_out)
    n = int(fs * 0.2)
    inband = feed(dec, complex_tone(1000, fs, n))
    # lands exactly on 1 kHz after decimation if not filtered
    alias = feed(dec.__class__(fs, d, 0.42 * fs_out), complex_tone(fs_out + 1000, fs, n))
    skip = 200
    p_in = np.mean(np.abs(inband[skip:]) ** 2)
    p_alias = np.mean(np.abs(alias[skip:]) ** 2)
    assert 10 * math.log10(p_alias / p_in) <= -60


def test_receiver_rejects_out_of_band_tone():
    n = int(FS * 1.0)
    vfo = 0.0
    chain_in = ReceiverChain(FS, AUDIO, "USB", offset=vfo, agc=no_agc())
    chain_out = ReceiverChain(FS, AUDIO, "USB", offset=vfo, agc=no_agc())
    a = feed(chain_in, complex_tone(1000, FS, n))[SETTLE:]
    b = feed(chain_out, complex_tone(48e3 + 1000, FS, n))[SETTLE:]
    assert 10 * math.log10(np.mean(b ** 2) / np.mean(a ** 2)) <= -60


@pytest.mark.parametrize("size", [1024, 8192, 65536])
def test_spectrum_bin_mapping(size):
    center = 100e6
    k_off = size // 8
    f_off = k_off * FS / size
    sa = SpectrumAnalyzer(size, frame_rate=30)
    frames = sa.process(complex_tone(f_off, FS, max(size * 4, int(FS / 10)), 0.5), center, FS)
    assert len(frames) >= 2
    fr = frames[-1]
    k = int(np.argmax(fr.db))
    assert k == size // 2 + k_off
    assert fr.bin_frequencies()[k] == pytest.approx(center + f_off, abs=1e-3)
    assert fr.frequency_to_bin(center + f_off) == pytest.approx(k)
    # amplitude 0.5 tone -> -6.02 dBFS peak
    assert fr.db[k] == pytest.approx(20 * math.log10(0.5), abs=0.1)


def test_agc_levels_signal():
    fs = 48000
    agc = Agc(fs, attack_ms=5, decay_ms=200)
    x = tone(1000, fs, fs * 2, 0.01)
    y = agc.process(x)
    peak = np.abs(y[fs:]).max()
    assert 20 * math.log10(peak) == pytest.approx(Agc.TARGET_DB, abs=1.5)


def test_wfm_stereo_separation():
    """Stretch goal: L = 1 kHz, R = 2.5 kHz through a real stereo MPX."""
    n = int(FS * 1.5)
    t = np.arange(n) / FS
    left = 0.8 * np.sin(2 * np.pi * 1000 * t)
    right = 0.8 * np.sin(2 * np.pi * 2500 * t)
    pilot = np.cos(2 * np.pi * 19000 * t)
    sub = np.cos(2 * np.pi * 38000 * t)
    mpx = 0.9 * ((left + right) / 2 + (left - right) / 2 * sub) + 0.1 * pilot
    iq = fm_modulate(mpx, FS, 75e3, offset=150e3) * np.float32(0.5) + awgn(n, -50)
    chain = ReceiverChain(FS, AUDIO, "WFM", offset=150e3, deemphasis_tau=0, stereo=True, agc=no_agc())
    out = feed(chain, iq)[SETTLE:]
    assert chain.stereo_detected
    sep_l = level_at(out[:, 0], AUDIO, 1000) - level_at(out[:, 0], AUDIO, 2500)
    sep_r = level_at(out[:, 1], AUDIO, 2500) - level_at(out[:, 1], AUDIO, 1000)
    assert sep_l > 25 and sep_r > 25, (sep_l, sep_r)
    mono = feed(ReceiverChain(FS, AUDIO, "WFM", offset=150e3, deemphasis_tau=0, stereo=False, agc=no_agc()),
                iq)[SETTLE:]
    assert abs(level_at(mono[:, 0], AUDIO, 1000) - level_at(mono[:, 0], AUDIO, 2500)) < 1.0


def test_no_stereo_without_pilot():
    n = int(FS * 1.0)
    iq = fm_modulate(tone(1000, FS, n, 0.5), FS, 75e3, offset=0) + awgn(n, -40)
    chain = ReceiverChain(FS, AUDIO, "WFM", offset=0, stereo=True, agc=no_agc())
    feed(chain, iq)
    assert not chain.stereo_detected


@pytest.mark.parametrize("pitch", [600.0, 800.0])
def test_cw_carrier_gives_pitch_tone(pitch):
    n = int(FS * 1.0)
    iq = complex_tone(70e3, FS, n, 0.05) + awgn(n, -70)
    out = feed(ReceiverChain(FS, AUDIO, "CW", offset=70e3, cw_pitch=pitch, agc=no_agc()), iq)[SETTLE:]
    f, pk, other = dominant(out, AUDIO)
    assert abs(f - pitch) <= 5 and pk - other >= 20


def test_dsb_and_raw():
    n = int(FS * 1.0)
    iq = complex_tone(-40e3 + 1000, FS, n, 0.1) + complex_tone(-40e3 - 1000, FS, n, 0.1)
    out = feed(ReceiverChain(FS, AUDIO, "DSB", offset=-40e3, agc=no_agc()), iq)[SETTLE:]
    assert abs(dominant(out, AUDIO)[0] - 1000) <= 5
    raw = feed(ReceiverChain(FS, AUDIO, "RAW", offset=-40e3, agc=no_agc()),
               complex_tone(-40e3 + 3000, FS, n, 0.1))[SETTLE:]
    assert raw.shape[1] == 2
    assert abs(dominant(raw[:, 0], AUDIO)[0] - 3000) <= 5
    # +3 kHz: Q lags... I = cos, Q = sin -> Q is I delayed by a quarter period
    c = np.correlate(raw[:4800, 0], raw[:4800, 1], "full")
    lag = np.argmax(c) - 4799
    assert abs(abs(lag) - 4) <= 1  # 48 kHz / 3 kHz / 4 = 4 samples
