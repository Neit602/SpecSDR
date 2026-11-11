# SpecSDR — design decisions

Each entry: what was decided, and why. Logged as the work progressed.

## Environment / dependencies
- **Python 3.14 venv at `.venv`** (system default interpreter). Code targets Python ≥ 3.10.
- **pyrtlsdr pinned to 0.3.0** (`requirements-hw.txt`). pyrtlsdr 0.4.0/0.5.x unconditionally bind
  `rtlsdr_set_dithering`, a symbol that exists only in some librtlsdr forks; with the widely used
  osmocom / RTL-SDR Blog library the *import itself* fails (`AttributeError`). Verified on this
  machine with `rtl-sdr-blog 1.3.6`. 0.3.0 binds only the standard API.
- pyrtlsdr 0.3.0 imports `pkg_resources`; `setuptools<81` is listed in `requirements-hw.txt`, and
  SpecSDR also installs a tiny stand-in `pkg_resources` module if it is missing (pyrtlsdr only uses it
  to read its own version string inside a try/except).
- All hardware imports are wrapped in `try/except Exception` (not just `ImportError`) because a
  library/binding mismatch raises other exception types at import time.

## DSP performance
- **Decimating FIR = strided view + BLAS matrix-vector product.** Measured on this machine
  (131072 complex64 samples): `scipy.signal.upfirdn` 1.33 ms vs strided matmul 0.74 ms for a
  24-tap/÷5 stage; 4.9 ms vs 2.5 ms for 64-tap/÷4. Both are stateful via an explicit history buffer.
- **NCO uses a cached rotator** (`exp(jωn)` for the block length, recomputed only when the
  frequency or block size changes) multiplied by a scalar start phase: one complex multiply per
  sample instead of a complex `exp` (0.04 ms vs 2.6 ms per 131k block). Phase is carried in float64.
- **DC removal = per-block mean, exponentially averaged, applied as a linear ramp across the block**
  (0.04 ms/block) instead of a per-sample IIR via `lfilter` (2 ms/block for complex input).
- 8-bit → complex64 conversion via `astype(float32)` + `view(complex64)` (0.07 ms/131k) rather
  than a 64k-entry lookup table (0.22 ms).
- **BLAS pinned to one thread** (`OPENBLAS_NUM_THREADS=1` etc., set in `specsdr/__init__.py` before
  numpy loads). Measured: with default OpenBLAS threading the process used ~1270 % CPU (spinning
  worker threads on tiny gemv calls); pinned it uses ~55 % with identical throughput.

## HackRF binding
- **Own receive-only ctypes binding to libhackrf** (`sources/_hackrf_ctypes.py`) instead of
  SoapySDR or a pip package. Reasons: (1) SoapySDR's Python bindings are not pip-installable;
  they come from the OS package manager and can't be used in a venv without
  `--system-site-packages`. (2) The pip wrappers (`pyhackrf2`, `python_hackrf`) are thin,
  rarely maintained, or need a compiler. (3) libhackrf's RX API is small and has been stable
  since 2014. (4) Binding only RX functions guarantees there is no TX path at all. Verified
  against `/usr/include/libhackrf/hackrf.h` of libhackrf 2026.01.3 (struct layouts of
  `hackrf_transfer` and `hackrf_device_list`).
- The libusb callback thread only copies the buffer into a bounded queue (overflows counted). A
  separate converter thread turns it into complex64 and also polls `hackrf_is_streaming` to detect
  an unplugged device.

## RTL-SDR
- pyrtlsdr handles enumeration, open/close and the async read loop. Control calls go directly to
  the `librtlsdr` function table that pyrtlsdr exposes: several pyrtlsdr 0.3.0 wrappers *close
  the device* on any error (e.g. `set_center_freq`), and `set_freq_correction` raises
  `NameError` on its error path. A harmless "unsupported" must not kill the device.
- Async reads (`rtlsdr_read_async`, ~50 callbacks/s, ~17 ms blocks at 2.4 MS/s) rather than sync reads: fewer USB gaps, low latency.
  When `read_async` returns without our having cancelled it, the device was lost and one error
  is reported.
- Offset tuning is offered but reports "not supported" on R820T/R828D (the library returns an
  error there). SpecSDR's **LO offset** mode (Source panel) is the generic way to keep the VFO off DC.
- RTL-SDR Blog V4 (R828D) lower limit set to 0.5 MHz (its driver switches to the built-in HF
  upconverter). Other tuners use their datasheet ranges; direct sampling gives 0.5–28.8 MHz.
- Confirm-required settings (bias-T, HackRF antenna power) are **never restored** from saved
  settings. They always start off.

## Threading / data flow
- Source thread → bounded `queue.Queue` (48 blocks) → DSP thread → audio jitter buffer → sound-card
  callback. A full queue drops the block and counts it; nothing ever blocks the source thread.
- The engine has no Qt imports. The GUI polls `take_frames()/take_events()/stats()` from QTimers
  in the GUI thread, which caps the update rate and cannot pile up queued signals when the GUI is
  slow (latest-state-wins). Parameter changes from the GUI are posted as commands and executed by
  the DSP thread between blocks, so DSP state is only ever touched by one thread.
- **Spectrum frames are computed at 2× the display rate**, the GUI timer runs at the display rate,
  and the engine **paces frame release over each block's duration**. A USB block (RTL: ~17 ms,
  HackRF at 2 MS/s: 65 ms) arrives all at once. Releasing all its frames at once aliased against the
  30 Hz GUI timer, and the display ran at 20–27 fps. Now it holds 30 fps at every rate tested
  (2–20 MS/s, both devices), at the cost of at most one block of extra display latency.
- Stopping a source always goes through a helper thread with a timeout (`Engine._call_with_timeout`),
  so a wedged USB driver can't hang the GUI.
- Device loss is detected three ways: the driver's own error, the read loop ending unexpectedly,
  and an engine **watchdog** (no samples for 3 s). Each reports one event, and the GUI shows one dialog.

## DSP design
- **Channelisation:** NCO mix → multistage decimator (integer factors, stages ≤ 8, picked from
  "smooth" decimations with prime factors ≤ 7, Kaiser 80 dB). Channel rate: ≥ 240 kHz for WFM,
  ≥ 48 kHz for the other modes. Then a windowed-sinc channel filter whose window and order the user
  can change. USB/LSB use a complex band-pass, so sideband selection is done by the filter and
  demodulation is just `Re{}`.
- **Clock-drift controller** regulates the *midpoint* of the jitter-buffer sawtooth (fill before a
  write + half the block) to the value it had when playback started, rather than to the nominal
  target. Steering to the nominal target bent the pitch by up to ±0.2 % for 10–30 s after every
  start, because the start-up fill depends on the source's block size (measured: +2000 ppm with
  HackRF at 2 MS/s). Holding the start value leaves only the real clock difference.
  Loop: proportional, ~30 s time constant, input smoothed. A 10 s loop amplified the ~20 ms
  period jitter of the sound server into ±1000 ppm swings (10-minute run). With 30 s, measured
  ranges are −7…+82 ppm (simulator) and −184…−1 ppm (RTL-SDR vs. this PC's sound card), i.e. well
  under 1 cent of pitch.
- **Audio resampling:** an integer FIR pre-decimator (which also does the sharp 15 kHz WFM low-pass),
  then a polyphase arbitrary-ratio resampler (64 phases, linear interpolation between phases). Its
  ratio can be nudged continuously. That is how **clock drift** between the SDR and the sound card
  is absorbed: a proportional controller keeps the jitter-buffer level constant, clamped to ±0.2 %.
- **WFM stereo:** the pilot is recovered by mixing the MPX down by a local 19 kHz oscillator,
  low-pass filtering to ±100 Hz and decimating. That gives the pilot's complex envelope `u`, and
  the 38 kHz carrier is `Re[(osc·u/|u|)²]`, with the MPX delayed by the filters' group delay.
  This is a feed-forward (open-loop) carrier recovery, **not a feedback PLL**: it is fully
  vectorised (a per-sample PLL loop in Python would cost far too much CPU), it locks immediately,
  and the ±100 Hz filter tracks any real pilot offset. Stereo is detected when the ±100 Hz band
  holds most of the power within ±2.4 kHz of 19 kHz (with hysteresis).
- **De-emphasis:** first-order, bilinear transform pre-warped at the corner frequency, so the
  response at 10 kHz matches the analogue τ = 50/75 µs filter (acceptance test within 1 dB).
- **AGC:** peak envelope with attack/decay/hang, threshold and slope. Gains are computed per 1 ms
  chunk aligned to the absolute sample count and ramped linearly inside the chunk, so the output
  never depends on block boundaries. The audio is delayed by one chunk so the gain for a chunk is
  known before it plays. This plus a ≤ 0.95 FS cap removes onset overshoot (measured: 200× overshoot
  before this change). Slope = output rise in dB per 10 dB input above the threshold.
- **Squelch:** channel power (same dBFS scale as the spectrum), 20 ms smoothing, 3 dB hysteresis,
  2 ms gain ramps (no clicks), chunk-aligned like the AGC, so it is block-size independent too.
- **Spectrum dBFS:** `|X|²/(Σw)²`, so a full-scale complex tone reads 0 dBFS for any FFT size or
  window. SNR = channel power − (median-bin noise × channel bandwidth / bin width / ENBW).
- NFM audio is low-passed at 5 kHz (the discriminator's noise rises with frequency). AM/SSB/CW audio is
  band-limited by the channel filter itself.
- Mode defaults (bandwidth, filter order, step) are in `dsp/receiver.py: MODE_INFO`. CW uses a
  1023-tap filter by default, because narrow CW filters need a long FIR at a 48 kHz channel rate.

## Display
- **Waterfall is a custom pyqtgraph GraphicsObject**, not `ImageItem`. It owns a ring-buffer QImage
  (≤ 8192 columns × 1024 rows, max-pooled from the FFT). A new line is one row of colour lookup
  written straight into the image memory, and a repaint is two `drawImage()` calls. `ImageItem`
  would re-convert the whole image every frame (`setImage` → `makeARGB` on ~8 M pixels).
  **Measured** (benchmark: 1500×450 widget, FFT 8192, 1024 rows, one new line plus a forced
  viewport repaint per iteration): custom item **1.29 ms** per line, `ImageItem` with a scrolling
  array **36.7 ms** per line. ImageItem alone could not keep up with 30 lines/s. The rest of the
  GUI is plain pyqtgraph. The item draws only the visible column range, so zooming costs nothing extra.
- x coordinates are Hz relative to the current centre frequency, so there are no float32 precision
  issues at GHz. The frequency axis places ticks on round absolute frequencies.
- When the centre frequency changes, the waterfall history is shifted, so old lines stay at their
  absolute frequencies. Changing the FFT size or sample rate clears it.
- The spectrum trace is max-pooled to the pixel width of the visible range. Max keeps narrow
  carriers at full height when many bins share a pixel.

## Simulated source
- Signals are synthesised at a low rate (content at an integer fraction of it), interpolated with
  one gemm per block (polyphase as a matrix product), and mixed to `f_signal − f_centre`. Noise comes
  from a pre-computed 1 M-sample complex Gaussian table read from random offsets. Measured cost:
  16 % of one core at 2.4 MS/s (was 37 % with an arbitrary resampler per signal).
- The NFM signal transmits in bursts (4 s on, 3 s off, with a 1 kHz tone at the start of each
  burst), so squelch can be tested. The CW beacon sends real Morse with 5 ms raised-cosine edges.

## Audio
- `sounddevice` is the output. If it is missing, or the device fails to open, SpecSDR falls back to
  `NullSink`, a thread that consumes audio at the real sample rate like a sound card, so buffering
  and drift correction behave the same.
- PortAudio stream latency is 80 ms, not "low". With "low" (a few ms) the Python callback missed
  deadlines whenever the DSP thread held the GIL. Measured: 4 device underflows in 8 s with
  "low", 0 in 30 s with 80 ms.

## Files
- IQ recordings are WAV (16-bit default; 8-bit unsigned and float32 selectable), with the centre
  frequency and rate in the filename and an `auxi` chunk (centre frequency, readable by other SDR
  programs). The header is rewritten every 8 MB, so a crash leaves a playable file. Files split before 4 GB.
- The file source reads via `np.memmap` (no load time, works for huge files), is paced in real time,
  loops, and seeks. Metadata comes from the filename (SpecSDR, SDR#-style, gqrx, and `100MHz`/`2.4Msps`
  tokens) or the WAV header; if either is missing, a dialog asks.
- Settings and bookmarks are JSON written atomically (temp file + rename).

## Testing method
- Device loss was tested on real hardware without unplugging: a USB port reset
  (`USBDEVFS_RESET` ioctl on the device node, allowed for the logged-in user) during streaming.
  The device drops off the bus and re-enumerates, so libusb fails exactly as on an unplug. Both
  devices: one dialog, stream stopped in 0.35 s (RTL) / 0.81 s (HackRF), GUI stall ≤ 14 ms, and a
  restart after re-enumeration works. A physical unplug is still on the manual checklist.
- The 10-minute "no audio gaps" test used the real sound card through sounddevice/PipeWire with
  the output muted. Muting multiplies by zero but the stream and its timing run normally, so the
  test made no noise on this machine.
- Bias-T and HackRF antenna power were only exercised on the *Cancel* path of their confirmation
  dialogs during hardware testing, so no DC was put on whatever antennas were connected.

## FPV video mode (analog FM video from a HackRF)

**What the project already had.** A `Source` abstraction with HackRF (20 MS/s sustained, 0 drops),
simulated and IQ-file sources; an `Engine` with a source thread, a bounded queue and a DSP thread that
posts Qt-free commands; `SpectrumAnalyzer`, `Mixer`, `DcRemover`, `FirFilter`, `AudioResampler`; a frequency
manager (JSON bookmarks); JSON settings saved via `Settings`; an SDR#-style side panel of collapsible
sections, a Recording panel (WAV only). So: no video code, no `.cs8` writing, no 20 MS/s in the simulator,
no `--frequency`/`--snapshot` options. Those were added first (they are small).

**Integration.**
- *FPV is a new mode ("FPV" in `MODES`), not a separate program.* It is entered from the mode buttons or the
  new "FPV Video" panel. The audio `ReceiverChain` is bypassed (the engine never builds one for FPV) but the
  spectrum and waterfall keep running. Entering sets 20 MS/s and a 20 MHz HackRF baseband filter and tunes the
  centre to the channel; leaving restores the mode, rate, filter, VFO and centre.
- *Threading.* The engine's DSP thread hands each raw (pre-DC-removal) block to `FpvWorker`, a decode thread
  with a bounded (120 ms) queue, and never blocks. When the decoder falls behind, whole blocks are dropped and the decoder is
  told (`notify_gap`), so it drops whole fields and resyncs rather than building a backlog. The GUI is updated
  through a Qt signal carried by `FpvBridge`, "latest wins" so Qt's event queue cannot pile up.
- *Channels live in the existing frequency manager.* User groups (1.3 GHz, 3.3 GHz, ...) are bookmarks with
  mode "FPV" in `bookmarks.json`; only the 5.8 GHz table (given by the task) is built in. No tables were
  invented for other bands.
- *Prerequisites added:* 16/20 MS/s in the simulator; `.cs8` recording (`RawIqWriter`, the same bytes as
  `hackrf_transfer -r`; `.cs8` playback already existed); `--frequency`, `--snapshot`, `--mode fpv`.
- *Simulated transmitters* synthesise real composite video (test pattern, sync, colour burst, PAL switch) and FM-modulate it at the sample rate.

**Decoder design (`specsdr/dsp/fpv/`, no Qt, no threads in the DSP itself).**
- *Fixed chunk grid.* Input is cut into chunks anchored to the sample stream, so any block size gives
  identical frames (this is an acceptance test, not an assumption). The front end (DC removal, AFC mixer,
  FM discriminator, video low-pass) runs one chunk ahead on a worker thread and the back end (sync, picture) consumes
  in order; the result does not depend on the threads.
- *numba justified by measurement.* `track_lines` 0.57 µs per line vs 150 µs in Python, `decode_lines` 16 µs vs
  15 ms in Python: without it a 20 MS/s video chain cannot run in real time in Python. Video FIR: numba 0.32 ms vs
  scipy `lfilter` 0.40 ms per 32768 samples. Discriminator: numpy `arctan2` is already 20 % of a core at 20 MS/s,
  and a numba arctan2 was not faster, so it stays numpy. numba and imageio-ffmpeg are pure pip wheels.
- *Sync.* A sync filter (0.8 µs moving average of the video) feeds a line PLL with a flywheel. Polarity is
  found from which sign gives a line-spaced pulse train (with a fallback flip when no vertical sync follows).
  Vertical sync is detected from half-lines with long low time; field parity from the half-line at which it starts;
  PAL vs NTSC from the line period (0.7 % apart) and confirmed by half-lines per field (625 vs 525). A sample-stream jump is found by looking for a pulse train at another phase while
  flywheeling, and a missed vsync is replaced by the expected one.
- *Colour.* The chroma oscillator is a function of absolute sample position, so hue is immune to line-timing jitter. A
  second-order loop on the burst locks it; cascaded moving averages low-pass the chroma (their first null lands
  near 2·fsc); PAL also averages two lines. Luma is the composite minus the re-modulated chroma ("chroma notch"),
  so there is no comb filter: high-frequency luma around the subcarrier is lost and shows as faint false colour (kept simple; see REPORT).
- *CNR estimate:* M2M4 moments of the IQ envelope (FM has a constant envelope, so all envelope variation is noise).
- *Picture reference in the PSNR tests* passes the source through the decoder's real luma response (video low-pass
  minus the chroma band), which is what "low-pass filtered to the decoder's video bandwidth" means for a colour decoder.
- *Scanner.* Detection is a FM-demodulated pulse-train test at the line period (not power), so CW, OFDM-like and noise never report. Range mode uses 8 MHz steps (≤ 4 MHz offset always). Detections within 7 MHz merge.
- *Latency:* the fixed chunk adds ≤ 6.6 ms; the bounded decode queue keeps the worst case ≤ 120 ms.
- *Chunk size changed after the first hardware test* from 32768 to 131072 samples (one HackRF USB transfer). Offline it is only 5–7 % faster, but live the small chunks lost about 12 % of frames, and the larger chunk removed the loss. The likely reason is fewer GIL re-acquisitions while other threads compete; that explanation was not measured directly. The pulse search limit (`MAX_PULSES`) went from 32 to 128 so a whole chunk's lines fit.
- *Sync watchdog.* Tracked tip/blanking levels update only on lines with a sync hit, so once a fade corrupts them, false hits can keep them wrong. Two consecutive half-field windows with under 50 % hits now restart acquisition, which measures levels afresh. One field of flywheel is kept for ordinary short losses.
