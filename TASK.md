# Task: Build PyRX — an SDR#-style receiver in Python

## Goal
Build a desktop software-defined-radio **receiver** in Python. Its layout, controls and workflow should closely follow SDR# (SDRSharp), but write everything from scratch. Do not copy SDR# code, icons, names or branding.

Supported hardware: **RTL-SDR** (RTL2832U with R820T/R828D tuners) and **HackRF One**. Receive only.

## How to work
- This is an **unattended run**: nobody will answer questions or approve steps. Work autonomously from this empty folder to a finished app. When something is ambiguous, pick the most reasonable option, log it in `DECISIONS.md`, and keep going. Stop early only if you are blocked by something only a human can do, and say exactly what that is in `REPORT.md`.
- Create and use a virtual environment at `.venv` in this folder. Do not install packages globally.
- There is **no time limit**. Only the quality of the finished app is judged, so do each part properly and verify it rather than rushing to cover everything. The result will be checked by a human with real hardware.
- Hardware may **not** be attached to this machine. The app must be fully developable and testable with the built-in simulated source and IQ file playback. If a device is attached, you may use it for extra testing.
- No fake features. Every visible control either works or is disabled with a "Not implemented" tooltip.
- Verify your own work: run the test suite, and launch the app headless (`QT_QPA_PLATFORM=offscreen`) to confirm it starts, streams from the simulated source for ~10 s, and exits cleanly.
- Work through the milestones in order. Finish and verify one before starting the next.

## Tech stack (fixed)
- Python 3.10+, **PyQt5**
- numpy + scipy for DSP
- pyqtgraph for spectrum and waterfall (a custom QPainter/OpenGL widget is allowed if it is measurably faster; justify in `DECISIONS.md`)
- sounddevice for audio output
- pyrtlsdr for RTL-SDR
- HackRF: choose a binding (e.g. SoapySDR Python bindings + SoapyHackRF, or a maintained pip package wrapping libhackrf). Justify the choice in `DECISIONS.md`.
- Hardware libraries and the audio library are **optional imports**. The app must start and run with the simulated source when they are missing (falling back to a silent null audio sink if needed), and show a clear message when a device can't be opened.
- `requirements.txt` must install cleanly with `pip install -r requirements.txt` in a fresh venv with no system libraries beyond Python. Put hardware bindings that need system libraries (librtlsdr, libhackrf, SoapySDR) in a separate `requirements-hw.txt`.
- A `README.md` with setup for **Linux** (packages for librtlsdr/libhackrf, udev rules, DVB driver blacklist) and **Windows** (Zadig/WinUSB, required DLLs).

## Architecture requirements
- Separate packages: `sources/` (device abstraction), `dsp/` (pure numpy/scipy, **no Qt imports**), `audio/`, `ui/`, `config/`.
- One common `Source` interface: open/close, start/stop, set center frequency, set sample rate, frequency and sample-rate ranges, and a declarative description of the device's gain/settings controls so the UI can build a per-device settings panel. Sources deliver `complex64` IQ blocks.
- Sources to implement:
  - **Simulated**: several configurable signals (AM, NFM, WFM broadcast, CW tone, SSB voice-like signal) plus noise, at fixed absolute frequencies so retuning behaves like real hardware.
  - **IQ file**: 2-channel WAV (8/16-bit int, float32) and raw `.cu8` / `.cs8` / `.cf32`; sample rate and center frequency from the filename or a dialog; loop option.
  - **RTL-SDR** and **HackRF One** (Milestone 2).
- Threading: acquisition and DSP never run on the GUI thread. Use queues/ring buffers between stages. The GUI is updated through Qt signals at a capped rate (~25–30 fps). Overflows and dropped blocks are counted and shown in the status bar, never crash the app.
- Every DSP block is **stateful across buffers** (filter state, NCO phase, previous demod sample, resampler state), so there are no clicks or discontinuities at block boundaries.

## Milestones

### M1 — Core receiver (simulated + file source)
- **Main window, SDR#-like:** collapsible left side panel with sections (Source, Radio, AGC, Audio, Display); top bar with Play/Stop, source selector, device configure button, large frequency display, volume slider; main area with spectrum above waterfall and a draggable splitter between them.
- **Frequency display:** large digits; mouse wheel over a digit changes that digit; clicking the upper/lower half of a digit increments/decrements it; direct keyboard entry.
- **Spectrum:** windowed FFT (Blackman-Harris default, selectable), FFT size 1024–65536, averaging/smoothing, dBFS scale, adjustable range and offset, optional peak hold, labelled frequency axis, zoom and pan.
- **Waterfall:** scrolling, SDR#-like color map, contrast/range controls, speed control, frequency axis aligned with the spectrum.
- **Tuning:** VFO separate from center frequency. Click spectrum/waterfall to tune the VFO. Filter passband drawn as a shaded region; drag its edges to change bandwidth; drag the region to retune. Mouse wheel tunes by the selected step; snap-to-step option. When the VFO leaves the visible span, the center frequency follows.
- **Demodulators:** WFM mono (de-emphasis 50 µs / 75 µs selectable), NFM, AM.
- **Audio:** output device selection, volume, mute, resampling to the sound card rate, jitter buffer that handles underruns/overruns without glitches piling up.
- Squelch with threshold slider.

### M2 — Hardware
- **RTL-SDR:** enumerate devices (index + serial); sample rates 0.25, 1.024, 1.4, 1.8, 1.92, 2.048, 2.4, 2.56, 2.88, 3.2 MS/s; tuner gain list read from the device plus tuner AGC; RTL AGC; frequency correction (ppm); direct sampling (I/Q branch) for HF; offset tuning; bias-tee if the library supports it.
- **HackRF One:** enumerate by serial; sample rates 2–20 MS/s; baseband filter bandwidth (auto/manual); LNA gain 0–40 dB in 8 dB steps; VGA gain 0–62 dB in 2 dB steps; RF amp on/off; antenna port power with a confirmation warning.
- **DC spike:** DC removal for both devices, plus an optional LO-offset mode so the VFO is never sitting on DC.
- **Errors:** device unplugged or read errors stop streaming with one clear message. No hang, no repeated traceback dialogs.
- **Performance:** decimate efficiently. Target: HackRF at 10 MS/s sustains spectrum + demodulation without continuous drops on a modern laptop. Measure and report the highest rate actually sustained.

### M3 — Full demodulator set and recording
- USB, LSB, CW (adjustable pitch/BFO), DSB, RAW (I/Q to audio L/R).
- AGC section: on/off, attack, decay, threshold, slope — applies to all modes.
- Per-mode default bandwidths and filter shape settings; bandwidth presets.
- Stretch: WFM stereo (19 kHz pilot PLL, stereo indicator).
- Recording: baseband IQ to WAV (center frequency and sample rate in filename) and demodulated audio to WAV, with elapsed time and file size shown.

### M4 — Usability
- Frequency manager: bookmarks with name, frequency, mode, bandwidth, group; "add current"; double-click to tune; stored as JSON.
- Settings persistence: last source and its settings, frequency, mode, bandwidth, FFT/waterfall settings, window geometry and splitter positions.
- Keyboard shortcuts (Space = play/stop, arrows = step tune, etc.). Dark theme.
- Status bar: signal level / SNR at the VFO, CPU %, effective sample rate, dropped blocks.

### Out of scope
- Any transmit functionality. HackRF TX must **not** be implemented.
- Digital-mode decoders, plugin system, rtl_tcp/network sources — only after everything above is done.

## Acceptance tests
Automated with pytest. No hardware and no audio device required (use a null audio sink).

1. **NFM:** synthesize NFM with a 1 kHz tone at +100 kHz offset, 2.4 MS/s. The demod chain tuned there outputs audio whose dominant frequency is 1 kHz ±5 Hz, at least 20 dB above any other spectral content.
2. **AM:** same with a 1 kHz tone at 80% modulation depth.
3. **SSB:** a tone at VFO + 1500 Hz demodulated in USB gives 1500 Hz audio; in LSB the same tone is attenuated by at least 30 dB.
4. **WFM:** synthesized WFM with a 1 kHz tone demodulates to 1 kHz with de-emphasis applied correctly (check relative level at 1 kHz vs 10 kHz test tones).
5. **Block continuity:** processing a signal as one block vs. many random-size blocks gives the same output (max abs difference < 1e-4).
6. **Spectrum mapping:** a tone at a known offset peaks at the correct bin and the axis labels map that bin to the correct frequency.
7. **Decimation:** an out-of-band tone is attenuated by at least 60 dB.
8. **File round trip:** record IQ from the simulated source, play it back through the file source, and get the same spectrum peak.
9. **Smoke:** `python -m pyrx --source simulated --autostart --exit-after 10` with `QT_QPA_PLATFORM=offscreen` streams for 10 s and exits with code 0, no exceptions logged.

Performance targets (measure and report): spectrum + waterfall ≥ 25 fps at 2.4 MS/s with FFT size 8192; GUI stays responsive while streaming; no audio gaps in 10 minutes of continuous WFM from the simulated source.

## Deliverables
- `python -m pyrx` launches the app; `pytest` passes.
- `README.md`: install, run, and a **manual hardware test checklist** for a human (WFM broadcast FM, NFM on a local repeater/PMR, AM airband, HackRF at 10 MS/s, unplugging a device mid-stream).
- `DECISIONS.md`: design choices and why.
- `REPORT.md` (write last): status of every feature above as done / partial / not done; how each was verified; test results; measured performance; known bugs. Be honest — an accurate report is valued more than a claimed feature that doesn't work.
