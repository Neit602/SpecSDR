"""FPV video acceptance tests. No hardware and no audio device needed: signals are synthesised
from the test pattern and FM-modulated at 20 MS/s. CNR is carrier power vs noise power across
the full 20 MHz."""

import numpy as np
import pytest

from fpv_helpers import (FS, NTSC, PAL, awgn, decode, decoder_luma_response, hue_deg, luma_psnr,
                         make_signal, read_counter)
from pyrx.dsp.fpv import kernels as k  # noqa: E402
from pyrx.dsp.fpv.decoder import FpvDecoder, FpvParams

STANDARDS = {"PAL": PAL, "NTSC": NTSC}


def locked_decode(std, **kw):
    x, tx = make_signal(std, **kw)
    d, frames = decode(x)
    return d, frames, tx


def check_standard_decode(d, frames, tx, std, min_psnr=28.0):
    field_samples = FS / std.field_rate
    clean = [f for f in d.field_log if f.clean]
    # locks within 3 fields
    assert clean, "no clean field decoded"
    assert clean[0].start <= 3 * field_samples
    assert d.stats.standard == std.name
    assert frames, "no frames"
    # the frame counter advances with no gaps
    counters = [read_counter(f.y, tx.encoder.pattern) for f in frames]
    assert all(b == a + 1 for a, b in zip(counters, counters[1:])), counters
    # decoded luma against the source, low-pass filtered to the decoder's video bandwidth
    f = frames[-1]
    n = counters[-1]
    ref = decoder_luma_response(tx.encoder.pattern.luma(n), std, d.params.video_bw)
    psnr, where = luma_psnr(f.y, ref)
    assert psnr >= min_psnr, (psnr, where)
    return psnr


@pytest.mark.parametrize("std", [PAL, NTSC], ids=["PAL", "NTSC"])
def test_decode_locks_and_matches_source(std):
    """PAL / NTSC at CNR 25 dB: lock within 3 fields, PSNR >= 28 dB, frame counter without gaps;
    the standard is auto-detected."""
    d, frames, tx = locked_decode(std, seconds=0.5, cnr=25)
    psnr = check_standard_decode(d, frames, tx, std)
    assert d.stats.h_locked and d.stats.v_locked
    assert d.stats.colour_locked
    print(f"{std.name}: PSNR {psnr:.1f} dB")


def test_ntsc_inverted_deviation_decodes():
    """The polarity of the discriminator output is detected automatically."""
    x, tx = make_signal(NTSC, seconds=0.4, cnr=25, inverted=True)
    d, frames = decode(x)
    assert d.stats.polarity == -1 and d.stats.standard == "NTSC"
    assert len(frames) >= 5
    counters = [read_counter(f.y, tx.encoder.pattern) for f in frames]
    assert all(b == a + 1 for a, b in zip(counters, counters[1:])), counters


def test_weak_signal_stays_locked():
    """CNR 12 dB: stays locked, at most 1 % lost lines."""
    x, tx = make_signal(PAL, seconds=1.0, cnr=12)
    d, frames = decode(x)
    st = d.stats
    assert st.h_locked and st.v_locked
    assert st.lines_total > 12000
    lost = st.lines_lost / st.lines_total
    assert lost <= 0.01, (st.lines_lost, st.lines_total)
    assert st.dropped_fields <= 2
    assert st.frames >= 20
    print(f"lost lines at 12 dB CNR: {100 * lost:.2f} %")


@pytest.mark.parametrize("cnr", [25.0, 12.0])
@pytest.mark.parametrize("offset", [1.5e6, -1.5e6])
def test_afc_pulls_in_offset_carrier(offset, cnr):
    """Carrier 1.5 MHz off nominal: AFC pulls it in and PSNR is within 3 dB of the on-frequency result."""
    x0, tx0 = make_signal(PAL, seconds=0.5, cnr=cnr)
    x1, tx1 = make_signal(PAL, seconds=0.5, cnr=cnr, offset=offset)
    d0, f0 = decode(x0)
    d1, f1 = decode(x1)
    assert f1, "no frames with carrier offset"
    assert abs(d1.stats.carrier_offset - offset) < 200e3, d1.stats.carrier_offset
    assert abs(d1.stats.afc_hz - offset) < 250e3

    def p(d, f, tx):
        n = read_counter(f[-1].y, tx.encoder.pattern)
        ref = decoder_luma_response(tx.encoder.pattern.luma(n), d.std, d.params.video_bw)
        return luma_psnr(f[-1].y, ref)[0]
    p0, p1 = p(d0, f0, tx0), p(d1, f1, tx1)
    assert p1 >= p0 - 3.0, (p0, p1)
    print(f"PSNR {p0:.1f} dB on frequency, {p1:.1f} dB with {offset / 1e6:+.1f} MHz offset")


def test_dropouts_relock_within_two_fields():
    """10 random chunks of 1000..100000 samples removed: re-lock within 2 fields after each gap."""
    x, tx = make_signal(PAL, seconds=1.5, cnr=25)
    rng = np.random.default_rng(7)
    n_gaps = 10
    lengths = rng.integers(1000, 100000, n_gaps)
    # gaps at random positions, at least 4 fields apart so each re-lock can be observed
    margin = int(4 * FS / PAL.field_rate)
    region = len(x) - 2 * margin
    cuts = np.sort(rng.choice(np.arange(margin, margin + region, 1000), n_gaps, replace=False))
    cuts = cuts + np.arange(n_gaps) * margin // 6
    cuts = np.minimum(cuts, len(x) - margin)
    keep = np.ones(len(x), bool)
    out_positions = []
    removed = 0
    for c, ln in zip(cuts, lengths):
        keep[c:c + int(ln)] = False
    y = x[keep]
    # positions of the gaps in the shortened stream
    removed = 0
    for c, ln in zip(sorted(cuts.tolist()), lengths[np.argsort(cuts)].tolist()):
        out_positions.append(c - removed)
        removed += int(ln)
    d, frames = decode(y)
    log = list(d.field_log)
    field_samples = FS / PAL.field_rate
    for g in out_positions:
        after = [f for f in log if f.start >= g and f.clean]
        assert after, f"no clean field after gap at {g}"
        assert after[0].start - g <= 2 * field_samples, (after[0].start - g) / field_samples
    assert len(frames) >= 15


def test_recovers_when_tracked_sync_levels_are_corrupted():
    """Seen on a real overheating VTX: a fade left the tracked sync levels wrong, false pulses kept
    them wrong, and the picture never came back although the signal did."""
    x, _ = make_signal(PAL, seconds=0.6, cnr=25)
    d = FpvDecoder(FS, FpvParams())
    cut = int(0.2 * FS)
    d.process(x[:cut])
    d.flush()
    height = d.st[k.ST_BLANK] - d.st[k.ST_TIP]
    assert height > 0
    d.st[k.ST_TIP] += 1.4 * height
    d.st[k.ST_BLANK] += 1.4 * height
    d.process(x[cut:])
    d.flush()
    field_samples = FS / PAL.field_rate
    after = [f for f in d.field_log if f.start >= cut + field_samples and f.clean]
    assert after, "picture never came back"
    assert after[0].start - cut <= 4 * field_samples, (after[0].start - cut) / field_samples


def test_line_tracker_behind_buffer_reacquires_instead_of_crashing():
    """A real HackRF run segfaulted in track_lines with the line grid 1.5 s behind the buffer."""
    x, _ = make_signal(PAL, seconds=0.6, cnr=25)
    d = FpvDecoder(FS, FpvParams())
    cut = int(0.2 * FS)
    d.process(x[:cut])
    d.flush()
    d.st[k.ST_NEXT] -= 30e6
    d.process(x[cut:])
    d.flush()
    assert d.counters["grid_lost"] == 1
    field_samples = FS / PAL.field_rate
    after = [f for f in d.field_log if f.start >= cut + field_samples and f.clean]
    assert after and after[0].start - cut <= 4 * field_samples


def test_lock_leaves_no_stale_pulses_for_the_rephase_search():
    """Pulses left over from acquisition sent the re-phase search seconds into the past."""
    x, _ = make_signal(PAL, seconds=0.2, cnr=25)
    d, _ = decode(x)
    assert d.counters["locks"] == 1
    assert not any(d._pulses.values())


def test_block_continuity_identical_frames():
    """One big block vs many random-size blocks gives identical frames."""
    x, tx = make_signal(PAL, seconds=0.4, cnr=25)
    d1, f1 = decode(x)
    d2 = FpvDecoder(FS)
    rng = np.random.default_rng(3)
    i = 0
    while i < len(x):
        n = int(rng.integers(1, 200000))
        d2.process(x[i:i + n])
        i += n
    d2.flush()
    f2 = d2.take_frames()
    assert len(f1) == len(f2) and len(f1) >= 8
    for a, b in zip(f1, f2):
        assert np.array_equal(a.rgb, b.rgb)
        assert np.array_equal(a.y, b.y)
    assert [(f.start, f.parity, f.clean) for f in d1.field_log] == [(f.start, f.parity, f.clean) for f in d2.field_log]


@pytest.mark.parametrize("std", [PAL, NTSC], ids=["PAL", "NTSC"])
def test_colour_bars_hue(std):
    """Each colour bar decodes to the source hue within +-10 degrees at CNR 25 dB."""
    x, tx = make_signal(std, seconds=0.4, cnr=25)
    d, frames = decode(x)
    assert d.stats.colour_locked
    f = frames[-1]
    pat = tx.encoder.pattern
    b = pat.band
    r0, r1 = b["BARS"] + 8, b["GREY"] - 8
    for i in range(1, 7):        # yellow, cyan, green, magenta, red, blue
        x0, x1 = pat.bar_edges[i] + 12, pat.bar_edges[i + 1] - 12
        rgb = np.mean(f.rgb[r0:r1, x0:x1], axis=(0, 1)) / 255.0
        src = pat.bar_rgb[i]
        h, hs = hue_deg(*rgb), hue_deg(*src)
        delta = (h - hs + 180) % 360 - 180
        assert abs(delta) <= 10, (i, h, hs, rgb)


def test_greyscale_fallback_without_burst():
    """A burst-less (monochrome) signal decodes as greyscale, no colour lock, no chroma."""
    from pyrx.dsp.filters import kaiser_lowpass
    from pyrx.dsp.fpv.pattern import FpvTransmitter
    tx = FpvTransmitter(PAL, FS)
    comp = tx.composite(int(0.4 * FS))
    taps = kaiser_lowpass(FS, 2.5e6, 3.5e6, 50.0).astype(np.float64)
    mono_c = np.convolve(comp, taps, mode="same").astype(np.float32)
    inst = (mono_c - np.float32(np.mean(mono_c))) * np.float32(8e6)
    ph = np.cumsum(inst.astype(np.float64)) * 2 * np.pi / FS
    x = np.exp(1j * ph).astype(np.complex64) + awgn(len(mono_c), 25)
    d, frames = decode(x)
    assert frames and not d.stats.colour_locked
    f = frames[-1].rgb
    assert np.abs(f[:, :, 0].astype(int) - f[:, :, 1].astype(int)).max() <= 1


def test_scanner_detects_video_not_other_signals():
    """detect_video: PAL/NTSC found by line structure; CW, OFDM-like and noise are not video."""
    from pyrx.dsp.fpv.detect import detect_video
    from pyrx.sources.simulated import SimulatedSource
    src = SimulatedSource()
    src.open()
    src.set_sample_rate(FS)
    keep = {"fpv_pal", "fpv_ntsc", "cw_58", "ofdm_58"}
    for s in src.signals:
        src.set_control(f"{s.key}.enabled", s.key in keep)

    def capture(freq, n=int(0.03 * FS)):
        src.set_center_frequency(freq)
        src.generate_block()  # flush the old centre
        parts = []
        have = 0
        while have < n:
            x, _, _ = src.generate_block()
            parts.append(x)
            have += len(x)
        return np.concatenate(parts)[:n]

    pal = detect_video(capture(5800e6), FS)
    ntsc = detect_video(capture(5917e6), FS)
    assert pal is not None and pal.standard == "PAL"
    assert ntsc is not None and ntsc.standard == "NTSC"
    assert detect_video(capture(5760e6), FS) is None
    assert detect_video(capture(5700e6), FS) is None
    assert detect_video(capture(5500e6), FS) is None   # noise only


def test_unlocked_raw_picture_and_no_lock_on_noise():
    """Noise only: no lock claimed; with show_raw the (noisy) picture is still produced."""
    n = int(0.3 * FS)
    x = awgn(n, -5.0, 11)
    d = FpvDecoder(FS, __import__("pyrx.dsp.fpv.decoder", fromlist=["FpvParams"]).FpvParams(show_raw=True))
    d.process(x)
    d.flush()
    frames = d.take_frames()
    assert frames and not frames[-1].locked
    assert not d.stats.h_locked and not d.stats.v_locked
    # the raw picture is noise, not a flat frame
    assert frames[-1].y.std() > 0.02
