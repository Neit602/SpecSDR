Task: Add analog FPV drone video reception to this SDR# clone
Context

This folder contains an existing SDR#-style software-defined-radio receiver written in Python with PyQt5, supporting RTL-SDR and HackRF One. Add the ability to receive, demodulate and display analog FPV drone video with the HackRF One, built into the existing app as a new mode — not a separate program.

How to work
Study the project first. Before changing any code, read the existing code and docs: entry point, package structure, source/device abstraction, HackRF source, DSP and threading model, UI layout and side-panel sections, settings persistence, frequency manager, IQ recording and file playback, tests, README, DECISIONS.md and REPORT.md if present. Add a short section to DECISIONS.md (create it if missing) describing what you found and how you will integrate FPV video.
Fit in, don't bolt on. Follow the existing architecture, code style, naming, dark theme and UI conventions. Reuse existing components (source abstraction, HackRF source, spectrum/waterfall, IQ recorder, file source, settings, frequency manager, status bar) instead of duplicating them. Where an existing component falls short of what FPV needs (for example sustained 20 MS/s from the HackRF), fix it and note the change in DECISIONS.md.
Don't break anything. Run the existing test suite before changing anything and record the baseline (passed / failed / skipped) in REPORT.md. At the end, every test that passed before must still pass. Don't refactor unrelated code.
Fill missing prerequisites. If the project lacks something this feature needs — HackRF support at 20 MS/s, a simulated source, IQ file playback of .cs8, headless command-line options — add it first, minimal and consistent with the existing code.
Environment. Use the project's existing virtual environment if it has one; otherwise create .venv in this folder. Do not install packages globally. Add new dependencies to the project's requirements file; they must be pip-installable without system libraries.
There is no time limit. Only the quality of the result is judged, so do each part properly and verify it rather than rushing. The result will be checked by a human with a real HackRF One and an analog FPV transmitter.
Hardware may not be attached to this machine. Develop and test with synthesized signals, the simulated source and IQ files.
No fake features. Every visible control either works or is disabled with a "Not implemented" tooltip.
Verify your own work: run the full test suite, launch the app headless (QT_QPA_PLATFORM=offscreen) in FPV mode, save decoded frames as PNG and look at them yourself to confirm the picture is right.
Technical constraints
numpy + scipy for DSP; numba is allowed for hot loops (e.g. sync detection and line resampling) when it is measurably faster — justify in DECISIONS.md.
Video DSP lives with the project's existing DSP code and has no Qt imports.
Decoding runs off the GUI thread; the GUI is updated through Qt signals. Every DSP block is stateful across buffers, so block boundaries cause no glitches.
Video recording uses a pip-installable encoder such as imageio-ffmpeg or opencv-python-headless (not opencv-python, whose bundled Qt clashes with PyQt5).
Background

Analog FPV video is composite video (PAL or NTSC) frequency-modulated onto a carrier, most often in the 5.8 GHz band, but also around 1.2–1.3 GHz, 2.4 GHz, 3.3 GHz and on non-standard frequencies. The signal is wideband (roughly 15–20 MHz), so it needs the HackRF at 20 MS/s.

Requirements
FPV Video mode: a new mode for HackRF, simulated and IQ-file sources, integrated SDR#-style: controls in a new "FPV Video" section of the side panel, video in a resizable, dockable panel with a full-screen option. For RTL-SDR the mode is disabled, with a tooltip explaining that its bandwidth and frequency range are too small. Entering the mode sets 20 MS/s and a suitable HackRF baseband filter, and tunes the center frequency to the channel; leaving it restores the previous mode and settings. Spectrum and waterfall keep running. If the sample rate is below 16 MS/s, warn that the picture will be degraded.
Channels: band + channel selector preloaded with the standard 5.8 GHz table below, manual frequency entry, and user-defined channel groups stored as JSON (for 1.2/1.3 GHz, 3.3 GHz and non-standard frequencies). Integrate with the existing frequency manager if there is one. Do not invent tables for other bands.
Band    1    2    3    4    5    6    7    8
A    5865    5845    5825    5805    5785    5765    5745    5725
B    5733    5752    5771    5790    5809    5828    5847    5866
E    5705    5685    5665    5645    5885    5905    5925    5945
F    5740    5760    5780    5800    5820    5840    5860    5880
R    5658    5695    5732    5769    5806    5843    5880    5917
L    5362    5399    5436    5473    5510    5547    5584    5621
(MHz)
Demodulation chain: DC removal → FM discriminator → AFC (estimate and correct the carrier offset so a transmitter up to ±1.5 MHz off nominal still decodes; show the measured offset) → video low-pass with configurable bandwidth (removes audio subcarrier and noise) → sync separation with automatic polarity detection (the discriminator output may be inverted) → horizontal PLL locked to the line rate with flywheel → vertical sync detection and field/frame assembly → black-level clamp from the back porch and video AGC → resampling of each line to a fixed pixel grid → deinterlacing (weave and line-doubling, selectable).
Standards: PAL (625 lines / 50 fields) and NTSC (525 / 59.94), auto-detected from line and field timing, with manual override.
Colour: PAL and NTSC chroma decoding locked to the colour burst. Fall back to greyscale automatically when the burst is missing or too weak. Colour on/off and saturation control.
Weak signals and dropouts: keep sync through noise and short dropouts (flywheel), and re-lock automatically within 2 fields after a dropout or dropped USB samples. When unlocked, the user can choose between showing the raw noisy picture or a "NO SIGNAL" screen.
Picture controls: brightness, contrast, saturation, video gain/AGC, sync threshold, horizontal/vertical position fine-adjust, aspect ratio 4:3 / 16:9.
Status overlay (toggleable): H/V lock, detected standard, colour lock, carrier offset, signal level / CNR estimate, decoded fps, dropped fields and samples.
Scanner: two modes — (a) all channels in the selected bands/groups, (b) a user-specified range (e.g. 1000–6000 MHz) in overlapping steps. At each step, detect analog video by its line-rate structure, not just by power, so CW carriers, Wi-Fi, digital FPV links and noise are not reported as video. Results list: frequency, nearest named channel, standard, signal level, last seen. Double-click tunes, optional auto-tune to the strongest, continuous re-scan mode. Measure and report the scan time for the 5.8 GHz table and for 1000–6000 MHz.
Audio subcarrier (stretch): FM audio subcarrier demodulation, subcarrier frequency selectable (6.5 MHz default, 6.0 and 5.5 MHz), played through the app's existing audio output.
Recording: snapshot to PNG; decoded video to MP4 (or AVI) with frequency and timestamp in the filename. IQ recording must work at 20 MS/s and include .cs8 output compatible with hackrf_transfer, so a capture can be replayed and decoded later through the file source.
Settings persistence: FPV settings (last channel, standard, picture controls, scanner ranges, panel layout) are saved and restored the same way as the app's other settings.
Simulated FPV transmitters: add to the simulated source at least a PAL transmitter at 5800 MHz (F4), an NTSC transmitter at 5917 MHz (R8) and one on a non-standard frequency, plus a CW carrier and a wideband noise-like (OFDM-like) signal in the 5.8 GHz band. The video is a test pattern of your own design: greyscale steps, resolution wedges, colour bars, a moving element and a frame counter. Deviation, CNR and frequency offset are configurable.
Performance: target every frame decoded in real time at 20 MS/s (25 fps PAL / 29.97 fps NTSC) with end-to-end latency under 200 ms. If the machine can't keep up, drop whole fields and resync cleanly rather than letting a backlog build. Measure and report decoded fps and latency.
Out of scope
Any transmit functionality. HackRF TX must not be implemented.
Digital FPV video systems (DJI, Walksnail/Avatar, HDZero) — proprietary digital links, not decodable with this approach. The scanner must not report them as analog video.
Acceptance tests

Add these to the project's pytest suite. No hardware and no audio device required. Signals are synthesized from the test pattern and FM-modulated at 20 MS/s; CNR means carrier power vs. noise power across the full 20 MHz.

PAL decode: at CNR 25 dB the decoder locks within 3 fields; decoded luma matches the source (low-pass filtered to the decoder's video bandwidth, positions aligned) with PSNR ≥ 28 dB; the frame counter advances with no gaps.
NTSC and auto-detection: the same for NTSC; the standard is auto-detected correctly; a signal with inverted deviation also decodes (polarity auto-detection).
Weak signal and offset: at CNR 12 dB the decoder stays locked with no more than 1% lost lines; with the carrier 1.5 MHz off nominal, AFC pulls it in and PSNR is within 3 dB of the on-frequency result.
Dropouts: removing 10 random chunks of 1,000–100,000 samples from the stream, the decoder re-locks within 2 fields after each gap and never raises.
Block continuity: decoding a signal as one block vs. many random-size blocks gives identical frames.
Colour: colour bars decode to the correct hues (each bar's mean hue within ±10° of the source) for PAL and NTSC at CNR 25 dB.
Scanner: with the simulated source carrying 2 video transmitters, a CW carrier and a wideband noise-like signal, the scanner reports exactly the 2 video channels with the correct standard.
File round trip: record 20 MS/s IQ as .cs8 from the simulated FPV transmitter, play it back through the file source, and decode the same test pattern.
Video smoke: using the app's existing entry point (add the options if missing), --source simulated --mode fpv --frequency 5800e6 --autostart --exit-after 10 --snapshot out.png with QT_QPA_PLATFORM=offscreen exits with code 0, logs no exceptions and writes a decoded frame showing the test pattern.
Deliverables
FPV Video mode working inside the existing app; the full test suite passes (all previously passing tests plus the new ones).
README.md: a new FPV video section — how to use the mode and scanner, a manual hardware test checklist for a human (an analog 5.8 GHz video transmitter on a known channel, the scanner finding it, a hackrf_transfer capture replayed and decoded through the file source, unplugging the HackRF mid-stream), and practical reception notes: antenna choice and polarisation, external LNA, RF amp and antenna port power settings.
DECISIONS.md: the integration decisions and why.
REPORT.md (update last; create if missing): a new FPV video section with the status of every requirement above as done / partial / not done, how each was verified, test results before and after your changes, measured performance, and known bugs. Be honest — an accurate report is valued more than a claimed feature that doesn't work.
