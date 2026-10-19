# PyRX — final report

**Status: all four milestones implemented; the full test suite (84 tests) passes; tested on real
RTL-SDR and HackRF hardware.** A few items could only be partly verified without a human (listening
to audio, physically unplugging a cable, Windows). They are marked below, and the README has a
manual checklist for them.

## Test environment

| | |
|---|---|
| Machine | Intel i5-12600KF (16 threads), 31 GB RAM, Arch Linux, Linux 7.2, GNOME/Wayland (+XWayland), PipeWire |
| Python | 3.14.7 (`.venv`), plus a fresh 3.11.15 venv for a second run of the whole suite |
| Libraries | PyQt5 5.15.11 (Qt 5.15.14), numpy 2.5.4, scipy 1.18.1, pyqtgraph 0.14.0, sounddevice 0.5.6, pyrtlsdr 0.3.0 |
| Hardware | **RTL-SDR Blog V4** (R828D, librtlsdr rtl-sdr-blog 1.3.6) and **HackRF One** r9 (firmware/libhackrf 2026.01.3). Both turned up during the run (they weren't in `lsusb` at the start). Indoor antenna; FM broadcast signals were weak on the RTL and moderate on the HackRF. |

At times the HackRF was held by another program on this machine (a process from a different
project folder). PyRX reported that cleanly ("Resource busy"). I waited until it was free and did
not touch the other program.

## Feature status

Legend: **done** = implemented and verified as described; **partial** = implemented, but some
part could not be verified here, or deviates from the spec in a stated way.

### M1 — Core receiver

| Feature | Status | How verified |
|---|---|---|
| SDR#-style main window: collapsible side panel (Source, Radio, AGC, Audio, Display, Recording, Frequency manager), top bar (Play/Stop, source selector, ⚙ configure, big frequency, volume/mute), spectrum over waterfall with draggable splitter | done | Screenshots inspected (offscreen grab + the real display); GUI tests |
| Frequency display: wheel per digit, click upper/lower half, keyboard digit entry, direct entry box (`145.5M`, `7074k`…) | done | `test_freq_click_upper_and_lower_half`, `test_freq_wheel_over_digit`, `test_freq_keyboard_entry`, `test_parse_frequency` |
| Spectrum: Blackman-Harris default plus 6 other windows, FFT 1024–65536, averaging and smoothing, dBFS, range/offset, peak hold, labelled MHz axis, zoom (slider, Ctrl+wheel) and pan (right/middle drag) | done | Acceptance 6 (`test_spectrum_bin_mapping`, `test_spectrum_peak_drawn_at_axis_frequency`, `test_freq_axis_labels_are_absolute`); screenshots at 1×, 4× and 40× zoom |
| Waterfall: scrolling, SDR#-like colour map (+3 others), contrast/range, speed 5–100 lines/s, x-axis locked to the spectrum, history shifts on retune | done | `test_waterfall_shifts_on_retune`, screenshots, benchmark below |
| Tuning: VFO separate from centre; click to tune; shaded passband; drag edges (symmetric or one-sided per mode) and drag the passband; wheel/arrow step tuning; snap to step; centre follows the VFO out of the span; zoomed view follows the VFO | done | `test_spectrum_click_and_drag_tuning`, `test_center_follows_when_vfo_leaves_span`, `test_main_window_basic_flow`, shortcut test |
| WFM mono with 50/75 µs de-emphasis, NFM, AM | done | Acceptance 1, 2, 4 (margins below); real FM via HackRF and RTL |
| Audio: output device selection, volume, mute, resampling to the card's rate, jitter buffer with underrun/overrun handling | done | Jitter-buffer unit tests; 10-minute run on the real sound card (0 underruns); device list populated from PortAudio |
| Squelch with threshold slider (plus live level readout) | done | Continuity test runs with squelch on; NFM bursts in the simulator; GUI |

### M2 — Hardware

| Feature | Status | How verified |
|---|---|---|
| RTL-SDR: enumerate (index + serial) | done | Real V4: `#0 Generic RTL2832U OEM SN: 00000001` |
| RTL-SDR sample rates 0.25–3.2 MS/s (the 10 listed) | done | Real device at 2.048, 2.4 and 1.024 MS/s; measured rate matches (2.400 MS/s) |
| Tuner gain list from the device, tuner AGC, RTL AGC | done | 29 gains read from the R828D; gain, tuner AGC (disables the slider) and RTL AGC changed live while streaming |
| Frequency correction (ppm) | done | Set live on hardware without error. The frequency shift itself was not measured against a reference. |
| Direct sampling (I/Q branch) | partial | Mode switch works on hardware (range becomes 0.5–28.8 MHz, centre re-clamped, streaming continues). HF reception wasn't verified: the V4 does HF through its upconverter, not direct sampling, and no HF antenna was attached. |
| Offset tuning | partial | Implemented. The attached R828D doesn't support it; PyRX shows one clear "not supported" message, verified on hardware. Not tested on an E4000/FC tuner. |
| Bias-tee | partial | Implemented with a confirmation dialog, never restored at start-up, switched off on close. Only the *Cancel* path was exercised on hardware, deliberately: no DC on unknown antennas. |
| HackRF: enumerate by serial | done | Real device listed by serial |
| HackRF 2–20 MS/s | done | Ran 2, 4, 10, 12.5, 16 and 20 MS/s on hardware; measured rate = nominal |
| Baseband filter auto/manual, LNA 0–40/8 dB, VGA 0–62/2 dB, RF amp | done | All changed live on hardware without errors (visual roll-off check left to the human checklist) |
| Antenna port power with confirmation | partial | Implemented (confirmation, never restored, forced off on close). Only the Cancel path was exercised on hardware, deliberately. |
| DC removal (both devices) and LO-offset mode | done | DC removal measured on a real HackRF capture: centre spike 34.6 dB above neighbouring bins → 3.4 dB below them (same code path for the RTL). LO offset: `test_lo_offset_keeps_vfo_off_dc`, GUI |
| Errors: unplug / read errors → one clear message, no hang | done* | Real hardware, using a USB port reset during streaming instead of pulling the cable (see DECISIONS.md). RTL: stopped in 0.35 s; HackRF: 0.81 s. Exactly one dialog, GUI stall ≤ 14 ms, restart after re-plug works. Also unit tests with a failing source and a silent (wedged) source (watchdog). *A physical unplug is on the checklist. |
| Performance: HackRF 10 MS/s with spectrum + demod, no continuous drops | done | 0 dropped blocks and 0 overflows at 10, 12.5, 16 and **20 MS/s**. See below. |

### M3 — Demodulators, AGC, recording

| Feature | Status | How verified |
|---|---|---|
| USB, LSB, CW (adjustable pitch), DSB, RAW (I→L, Q→R) | done | Acceptance 3 (SSB), `test_cw_carrier_gives_pitch_tone` (600/800 Hz), `test_dsb_and_raw` (incl. I/Q quadrature) |
| AGC: on/off, attack, decay, threshold, slope (+ hang, + manual gain when off), all modes | done | `test_agc_levels_signal`, continuity test with AGC on, GUI |
| Per-mode default bandwidths, filter window and order, bandwidth presets | done | `MODE_INFO` table; values persist per mode; GUI |
| Stretch: WFM stereo with pilot detection and STEREO indicator | done (deviation) | Separation **31.9 / 31.8 dB** (L/R); no false stereo without a pilot; real stations at 101.0 and 101.6 MHz decoded as stereo (HackRF). The pilot is recovered with a feed-forward filter, **not a feedback PLL**; see DECISIONS.md. |
| Recording: baseband IQ to WAV (centre and rate in the filename), audio to WAV, elapsed time and size shown | done | Acceptance 8, `test_iq_record_then_play_back_in_window`, audio recording in `test_main_window_basic_flow` |

### M4 — Usability

| Feature | Status | How verified |
|---|---|---|
| Frequency manager: name, frequency, mode, bandwidth, group; add current; double-click to tune; JSON | done | `test_bookmarks_roundtrip`, `test_bookmark_tunes_frequency_mode_and_bandwidth` |
| Settings persistence (source and its settings, frequency, mode, bandwidth, FFT/waterfall, geometry, splitters, sections) | done | `test_settings_roundtrip`, `test_main_window_basic_flow` (reopens the saved file) |
| Keyboard shortcuts (Space, arrows, Alt+1…8, …) | done | `test_space_shortcut_and_mode_shortcut` (real key events); list under *Help → Keyboard shortcuts* |
| Dark theme | done | Screenshots |
| Status bar: level, SNR, CPU %, effective sample rate, dropped blocks (plus audio buffer, fps) | done | Values checked during every hardware and simulator run |

## Tests

`pytest`: **84 passed** (27 DSP acceptance, 12 engine/sources, 30 tuning/I/O/config, 15 GUI).
Runs headless, needs no hardware and no sound card. The final run on the final code is green.
Stability: after fixing an intermittent pyqtgraph teardown segfault (see below), 3 consecutive
full-suite runs and 8 GUI-only runs were all green; the only later code change was the
drift-loop gain. The suite also passed in a fresh Python 3.11 venv (before the last few fixes).

*Found and fixed during testing:* one full-suite run segfaulted in Qt's graphics scene during GUI
test teardown. A queued scene update reached pyqtgraph items of an already-closed window. PyRX now
closes its plot widgets explicitly when a window closes (`SpectrumPanel.shutdown`). It didn't
recur in 11 runs after that, but it was intermittent, so it is listed under known issues too.

| # | Acceptance test | Requirement | Measured |
|---|---|---|---|
| 1 | NFM 1 kHz at +100 kHz, 2.4 MS/s | 1 kHz ± 5 Hz, ≥ 20 dB above the rest | 1000.00 Hz, **52.3 dB** |
| 2 | AM 1 kHz, 80 % | same | 1000.00 Hz, **96.1 dB** |
| 3 | SSB tone at VFO + 1500 Hz | USB → 1500 Hz; LSB ≥ 30 dB down | 1500.00 Hz; LSB rejection **106 dB** |
| 4 | WFM, 1 kHz vs 10 kHz, de-emphasis | correct relative level | τ = 50 µs: −10.02 dB (theory −9.95); τ = 75 µs: −12.86 dB (theory −12.79) |
| 5 | One block vs random blocks | max\|diff\| < 1e-4 | **1.0e-6** worst over all 8 modes (AGC + squelch on) |
| 6 | Spectrum peak bin and axis label | correct | Exact bin at 1k/8k/64k FFT; amplitude −6.02 dBFS for a 0.5 tone; axis label = frequency |
| 7 | Out-of-band (aliasing) tone | ≥ 60 dB | **105.6 dB** (2.4 MS/s ÷50), **108.3 dB** (10 MS/s ÷200) |
| 8 | Record sim IQ → file source → same peak | same peak | Identical peak bin for 16-bit, 8-bit and float WAV |
| 9 | `python -m pyrx --source simulated --autostart --exit-after 10` (offscreen) | exit 0, no exceptions | Exit 0, ~490 blocks, 0 drops, no ERROR/Traceback. Also checked in a fresh venv. |

`requirements.txt` was checked in a brand-new venv (`pip install -r requirements.txt`, no system
libraries needed). The smoke command then ran from that venv.

## Performance (measured on this machine)

| Scenario | Result |
|---|---|
| Simulator 2.4 MS/s, FFT 8192, WFM stereo, offscreen | **30.3 fps** steady; CPU ~60 % of one core (DSP thread ~28 %) |
| Same on the real desktop display (XWayland) | **30.3 fps** (min 30.3); CPU ~60 %; worst GUI timer lag 29 ms |
| RTL-SDR V4 at 2.4 MS/s, WFM | 30.3 fps; 2.400 MS/s measured; 0 drops; CPU ~50 %; worst GUI lag 34 ms |
| HackRF 10 MS/s, WFM + spectrum | 30.3 fps; 10.00 MS/s; **0 drops / 0 overflows**; CPU 61–73 %; DSP thread ~50 %; GUI lag ≤ 9 ms |
| HackRF 12.5 / 16 / 20 MS/s | 0 drops at all three; at **20 MS/s** (the device maximum): CPU ~85 %, DSP thread 58–69 %, 30 fps |
| **Highest rate sustained** | **20 MS/s** (HackRF's maximum), with spectrum + WFM demodulation and no drops (30 s runs) |
| HackRF 2 MS/s (65 ms USB blocks) | 30.3 fps (frame pacing), 0 drops |
| DSP chain alone (no GUI) | 10 MS/s WFM uses 27 % of one core; 20 MS/s uses 42 % |
| Waterfall line + repaint (1500×450, FFT 8192) | custom item **1.29 ms** vs pyqtgraph ImageItem 36.7 ms |
| **10 min continuous WFM, simulator, real sound card (muted)** | see "10-minute audio run" below |

Before / after measurements behind several design changes (BLAS threads, PortAudio latency,
frame pacing, drift control) are in DECISIONS.md.

### 10-minute audio run

`python -m pyrx --source simulated --autostart --exit-after 600 --stats`, WFM stereo at
99.8 MHz, real sound card (sounddevice → PipeWire, 48 kHz), output muted (the stream and its
timing run normally; it just plays zeros), final code:

| Metric | Result |
|---|---|
| Duration / blocks | 599.6 s / 29,966 blocks |
| Audio underruns (jitter buffer + PortAudio device underflows) | **0** |
| Audio overruns | **0** |
| Dropped blocks / source overflows / DSP errors | 0 / 0 / 0 |
| Jitter-buffer fill | 83–121 ms (target 120 ms) |
| Clock-drift correction | −3…+246 ppm (avg +66 ppm, < 0.5 cent of pitch) |
| Display | 30.3 fps average, 30.2 minimum (1 s windows) |
| CPU | 45 % of one core on average (DSP thread 22 %) |
| Worst GUI timer lag | 16 ms |

Two earlier 10-minute runs, made before the last drift-loop and frame-pacing changes, also had 0
underruns. The first attempt with PortAudio "low" latency did **not**: 4 device underflows in
the first 8 s. That led to the 80 ms device buffer (DECISIONS.md).

## Known bugs and limitations

- **Not checked by ear.** All audio checks are numerical (tone purity, separation, spectrograms
  of real FM audio, underrun counters). Real-world sound quality needs the human checklist.
- **SNR readout is approximate.** The noise floor is the median spectrum bin, so in a crowded band
  (e.g. FM broadcast at 10–20 MS/s) it is overestimated and the SNR shown is low.
- **Stereo pilot recovery is feed-forward (filter + squaring), not a feedback PLL.** It works and
  measures well, but it isn't literally the "PLL" the task names.
- Waterfall history is max-pooled to at most 8192 columns. With FFT sizes 16k–64k the spectrum
  trace has full resolution, but the waterfall is coarser when deeply zoomed.
- Changing the FFT size or sample rate clears the waterfall history.
- After a device is lost, PyRX stops and tells you once. It does not reconnect automatically:
  re-plug, press ⟳, then Start.
- The simulator synthesises in real time. At 10 MS/s that costs ~60 % of a core by itself, which is
  why the high-rate figures were measured with the HackRF.
- Windows support (DLL loading, Zadig instructions) is written and reasoned through but **untested**
  (no Windows machine). macOS untested.
- Not tested with real signals: NFM repeater/PMR, AM airband, CW/SSB on the air, HF via direct
  sampling (no suitable antenna or activity during the run). They work on the simulator and in the
  acceptance tests, and are on the manual checklist.
- The ppm correction was set on hardware but its frequency effect wasn't measured against a reference.
- An intermittent segfault at GUI teardown was seen once in the test suite (pyqtgraph scene
  update after a window closed) and fixed. It didn't recur in 11 runs; never seen in the app itself
  (all ~30 app runs exited with code 0).
- librtlsdr prints its own diagnostics to stderr (e.g. "Found Rafael Micro R828D tuner",
  "cb transfer status: 5, canceling..." on device loss). These are not PyRX errors.
- pyrtlsdr must be 0.3.0 (newer versions don't import against the standard librtlsdr), and it
  emits a `pkg_resources` deprecation notice, which PyRX suppresses.

## Nothing blocked

No step required a human. Hardware testing was opportunistic: both devices turned up during the
run and were tested.
