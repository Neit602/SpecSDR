"""Simulated receiver: a handful of signals at fixed absolute frequencies plus noise.

Each signal is synthesised at a low rate, interpolated to the device rate and shifted to
(signal frequency - centre frequency), so retuning behaves like real hardware: signals stay
put on the frequency axis and move out of the band when you tune away."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

import numpy as np

from ..dsp.filters import FirInterpolator, IirFilter, kaiser_lowpass
from ..dsp.nco import Mixer
from .base import Control, DeviceInfo, Source, SourceError

# ----------------------------------------------------------------------------- content


class VoiceSynth:
    """Voice-like audio: a gliding harmonic series shaped by two moving formants, in
    syllables with gaps. Deterministic (seeded per syllable). Returns the analytic
    signal (only positive frequencies), so its real part is the real audio."""

    def __init__(self, fs: float, seed: int, band=(250.0, 2800.0)):
        self.fs = fs
        self.seed = seed
        self.band = band
        self._n = 0
        self._phase = 0.0
        self._idx = -1
        self._start = 0
        self._end = 0
        self._p = None

    def _next_syllable(self):
        self._idx += 1
        rng = np.random.default_rng([self.seed, self._idx])
        if rng.random() < 0.2:
            dur, p = rng.uniform(0.12, 0.45), None
        else:
            dur = rng.uniform(0.10, 0.30)
            p = dict(f0a=rng.uniform(100, 180), f0b=rng.uniform(95, 190),
                     f1=rng.uniform(300, 850), f2=rng.uniform(900, 2400), amp=rng.uniform(0.5, 1.0))
        self._start = self._n
        self._end = self._n + max(1, int(dur * self.fs))
        self._p = p

    def generate(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.complex64)
        i = 0
        while i < n:
            if self._n >= self._end:
                self._next_syllable()
            take = min(n - i, self._end - self._n)
            p = self._p
            if p is not None:
                length = self._end - self._start
                frac = (np.arange(take) + (self._n - self._start)) / length
                f0 = p["f0a"] + (p["f0b"] - p["f0a"]) * frac
                ph = self._phase + 2 * np.pi * np.cumsum(f0) / self.fs
                self._phase = float(ph[-1] % (2 * np.pi))
                fm = float(f0.mean())
                ks = np.arange(1, int(self.band[1] / fm) + 1)
                fk = ks * fm
                a = (np.exp(-((fk - p["f1"]) / 180.0) ** 2) + 0.6 * np.exp(-((fk - p["f2"]) / 260.0) ** 2) + 0.08)
                a *= np.clip((fk - self.band[0] * 0.7) / (self.band[0] * 0.6), 0, 1)
                a /= math.sqrt(np.sum(a ** 2)) + 1e-9
                env = np.sin(np.pi * frac) ** 1.5 * p["amp"]
                sig = (a[:, None] * np.exp(1j * ks[:, None] * ph[None, :])).sum(axis=0)
                out[i:i + take] = (env * sig * 0.7).astype(np.complex64)
            else:
                self._phase = float((self._phase + 2 * np.pi * 120 * take / self.fs) % (2 * np.pi))
            self._n += take
            i += take
        return out


class MusicSynth:
    """Stereo 'music': chords on the left, a melody with vibrato on the right."""

    CHORDS = [(261.6, 329.6, 392.0), (196.0, 246.9, 392.0), (220.0, 261.6, 329.6), (174.6, 220.0, 349.2)]
    MELODY = [523.3, 587.3, 659.3, 784.0, 659.3, 587.3, 523.3, 440.0, 493.9, 523.3, 659.3, 587.3]

    def __init__(self, fs: float):
        self.fs = fs
        self._n = 0

    def generate(self, n: int) -> np.ndarray:
        t = (np.arange(n) + self._n) / self.fs
        self._n += n
        bar = (t // 2.0).astype(int) % len(self.CHORDS)
        left = np.zeros(n)
        for j in range(3):
            f = np.array([c[j] for c in self.CHORDS])[bar]
            left += np.sin(2 * np.pi * f * t) * 0.22 + np.sin(4 * np.pi * f * t) * 0.06
        beat = t / 0.25
        note = (beat.astype(int)) % len(self.MELODY)
        env = np.exp(-3.0 * (beat % 1.0))
        fm = np.array(self.MELODY)[note]
        vib = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * t)
        # phase from the instantaneous frequency would need state; notes are short enough
        # that an absolute-time phase is fine (vibrato as phase modulation)
        right = np.sin(2 * np.pi * fm * t * vib) * env * 0.5 + np.sin(2 * np.pi * 2 * fm * t) * env * 0.1
        # a little "cymbal" noise on every beat for some treble content
        rng = np.random.default_rng(self._n)
        hiss = rng.standard_normal(n) * np.exp(-12.0 * (beat % 1.0)) * 0.04
        return np.stack([left + hiss, right + hiss], axis=1).astype(np.float32)


def morse_envelope(text: str, wpm: float, fs: float, gap_s: float = 3.0) -> np.ndarray:
    code = {"A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
            "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
            "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
            "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..", "0": "-----",
            "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
            "7": "--...", "8": "---..", "9": "----.", "/": "-..-."}
    unit = int(round(1.2 / wpm * fs))
    parts = []
    for w, word in enumerate(text.upper().split()):
        if w:
            parts.append(np.zeros(unit * 4))  # 7 units between words (3 already after a letter)
        for ch in word:
            for s in code.get(ch, ""):
                parts.append(np.ones(unit * (3 if s == "-" else 1)))
                parts.append(np.zeros(unit))
            parts.append(np.zeros(unit * 2))
    parts.append(np.zeros(int(gap_s * fs)))
    env = np.concatenate(parts)
    # 5 ms raised-cosine keying edges (no key clicks)
    r = max(1, int(0.005 * fs))
    k = np.hanning(2 * r + 1)
    env = np.convolve(env, k / k.sum(), mode="same")
    return env.astype(np.float32)


# ----------------------------------------------------------------------------- signals

@dataclass
class SimSignal:
    key: str
    kind: str          # wfm / nfm / am / cw / usb
    frequency: float
    level_db: float
    label: str
    enabled: bool = True


DEFAULT_SIGNALS = [
    SimSignal("wfm", "wfm", 99.8e6, -25.0, "WFM stereo broadcast"),
    SimSignal("nfm", "nfm", 100.25e6, -45.0, "NFM voice (repeater bursts)"),
    SimSignal("cw", "cw", 100.45e6, -55.0, "CW beacon"),
    SimSignal("am", "am", 100.6e6, -40.0, "AM voice, 80 %"),
    SimSignal("usb", "usb", 100.75e6, -50.0, "USB voice"),
]


class _Path:
    """One signal: generator at a low rate -> interpolator -> mixer to its offset."""

    BW = {"wfm": 260e3, "nfm": 16e3, "am": 12e3, "cw": 1e3, "usb": 4e3}

    def __init__(self, sig: SimSignal, fs: float, k: int, seed: int):
        self.sig = sig
        self.k = k
        self.rate = fs / k
        r = self.rate
        bw = self.BW[sig.kind]
        self.interp = None
        if k > 1:
            self.interp = FirInterpolator(kaiser_lowpass(fs, bw / 2, max(r - bw / 2, bw / 2 * 1.3), 70.0), k)
        self.mixer = Mixer(fs, 0.0)
        self.amp = 10 ** (sig.level_db / 20.0)
        self._n = 0
        kind = sig.kind
        # audio content is synthesised at an integer fraction of the path rate and
        # interpolated up (one gemm) - much cheaper than an arbitrary resampler here
        if kind in ("nfm", "am", "usb"):
            self.kv = max(1, int(round(r / 8000.0)))
            self.voice = VoiceSynth(r / self.kv, seed)
            self.vres = FirInterpolator(kaiser_lowpass(r, 3300.0, r / self.kv - 3300.0, 60.0), self.kv) \
                if self.kv > 1 else None
        if kind == "wfm":
            self.km = max(1, int(round(r / 48000.0)))
            self.music = MusicSynth(r / self.km)
            self.mres = FirInterpolator(kaiser_lowpass(r, 15500.0, r / self.km - 15500.0, 60.0), self.km) \
                if self.km > 1 else None
            self.mres_r = FirInterpolator(kaiser_lowpass(r, 15500.0, r / self.km - 15500.0, 60.0), self.km) \
                if self.km > 1 else None
            self.pre = [self._preemph(r) for _ in range(2)]
            self.pilot = Mixer(r, 19000.0)
            self._phase = 0.0
        if kind == "nfm":
            self._phase = 0.0
        if kind == "cw":
            self.key_env = morse_envelope("CQ CQ CQ DE PYRX PYRX K", 18.0, r)

    @staticmethod
    def _preemph(fs: float, tau: float = 50e-6) -> IirFilter:
        from scipy import signal
        wp = 2 * math.pi * 25000.0
        b, a = signal.bilinear([tau * wp, wp], [1.0, wp], fs)
        return IirFilter(b, a)

    def _voice(self, n: int) -> np.ndarray:
        self._vbuf = getattr(self, "_vbuf", np.zeros(0, np.complex64))
        need = n - len(self._vbuf)
        if need > 0:
            v = self.voice.generate(int(math.ceil(need / self.kv)))
            if self.vres is not None:
                v = self.vres.process(v)
            self._vbuf = np.concatenate([self._vbuf, v])
        out, self._vbuf = self._vbuf[:n], self._vbuf[n:]
        return out

    def generate_low(self, n: int) -> np.ndarray:
        kind = self.sig.kind
        t0 = self._n
        self._n += n
        if kind == "usb":
            return self._voice(n) * np.float32(1.2)
        if kind == "am":
            audio = self._voice(n).real * 1.25
            audio = np.clip(audio, -1, 1)
            return (1.0 + 0.8 * audio).astype(np.complex64)
        if kind == "nfm":
            t = (np.arange(n) + t0) / self.rate
            cyc = t % 7.0
            on = np.clip(np.minimum(cyc / 0.02, (4.0 - cyc) / 0.02), 0, 1)
            beep = (cyc < 0.4)
            audio = np.where(beep, 0.6 * np.sin(2 * np.pi * 1000 * t), self._voice(n).real * 1.4)
            inst = 2500.0 * np.clip(audio, -1.2, 1.2)
            ph = self._phase + 2 * np.pi * np.cumsum(inst) / self.rate
            self._phase = float(ph[-1] % (2 * np.pi))
            return (on * np.exp(1j * ph)).astype(np.complex64)
        if kind == "cw":
            idx = (np.arange(n) + t0) % len(self.key_env)
            return self.key_env[idx].astype(np.complex64)
        if kind == "wfm":
            # stereo MPX: (L+R)/2 + pilot + (L-R)/2 * cos(2*pilot), 75 kHz deviation
            self._mbuf = getattr(self, "_mbuf", np.zeros((0, 2), np.float32))
            need = n - len(self._mbuf)
            if need > 0:
                mm = self.music.generate(int(math.ceil(need / self.km)))
                if self.mres is not None:
                    mm = np.stack([self.mres.process(mm[:, 0]), self.mres_r.process(mm[:, 1])], axis=1)
                self._mbuf = np.concatenate([self._mbuf, mm])
            m, self._mbuf = self._mbuf[:n], self._mbuf[n:]
            left = self.pre[0].process(m[:, 0])
            right = self.pre[1].process(m[:, 1])
            p = self.pilot.generate(n)
            p38 = (p * p).real
            mpx = 0.9 * ((left + right) * 0.5 + (left - right) * 0.5 * p38) + 0.1 * p.real
            ph = self._phase + 2 * np.pi * 75000.0 * np.cumsum(mpx) / self.rate
            self._phase = float(ph[-1] % (2 * np.pi))
            return np.exp(1j * ph).astype(np.complex64)
        raise ValueError(kind)

    def generate(self, n_out: int, offset: float) -> np.ndarray:
        x = self.generate_low(n_out // self.k)
        if self.interp is not None:
            x = self.interp.process(x)
        self.mixer.set_frequency(offset)
        x = self.mixer.process(x)
        x *= np.float32(self.amp)
        return x


class SimulatedSource(Source):
    name = "simulated"
    display_name = "Simulated"

    RATES = [0.25e6, 1.024e6, 1.4e6, 2.048e6, 2.4e6, 3.2e6, 5e6, 8e6, 10e6]

    def __init__(self):
        self.signals = [SimSignal(**vars(s)) for s in DEFAULT_SIGNALS]
        super().__init__()
        self._center = 100.0e6
        self._rate = 2.4e6
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paths: list[_Path] = []
        self._rebuild = True
        rng = np.random.default_rng(1234)
        n = 1 << 20
        self._noise = ((rng.standard_normal(n, np.float32) + 1j * rng.standard_normal(n, np.float32))
                       * np.float32(math.sqrt(0.5))).astype(np.complex64)
        self._rng = np.random.default_rng(99)
        self.blocks_generated = 0

    @classmethod
    def enumerate(cls):
        return [DeviceInfo("sim0", "Simulated receiver")]

    # ------------------------------------------------------------------ controls
    def controls(self):
        c = [
            Control("noise_db", "Noise level", "slider", -45.0, minimum=-90, maximum=-10, step=1, unit="dBFS",
                    tooltip="Total noise power across the whole sample rate"),
            Control("dc_spike", "Simulate DC offset / LO leakage", "bool", False,
                    tooltip="Adds a DC component like real hardware has, to try DC removal and LO offset"),
        ]
        for s in self.signals:
            c.append(Control(f"{s.key}.enabled", "Enabled", "bool", s.enabled, group=s.label))
            c.append(Control(f"{s.key}.freq", "Frequency", "number", s.frequency / 1e6, minimum=0.1,
                             maximum=6000, step=0.005, decimals=4, unit="MHz", group=s.label))
            c.append(Control(f"{s.key}.level", "Level", "slider", s.level_db, minimum=-100, maximum=0,
                             step=1, unit="dBFS", group=s.label))
        return c

    def _apply_control(self, key, value):
        if "." in key:
            sk, attr = key.split(".", 1)
            for s in self.signals:
                if s.key == sk:
                    if attr == "enabled":
                        s.enabled = bool(value)
                    elif attr == "freq":
                        s.frequency = float(value) * 1e6
                    elif attr == "level":
                        s.level_db = float(value)
                    self._rebuild = True

    # ------------------------------------------------------------------ device
    def open(self, device_id=None):
        self._open = True

    def close(self):
        self.stop()
        self._open = False

    def frequency_range(self):
        return 100e3, 6e9

    def sample_rates(self):
        return list(self.RATES)

    def _hw_set_rate(self, hz):
        self._rebuild = True
        return hz

    def _build_paths(self):
        fs = self._rate
        paths = []
        for i, s in enumerate(self.signals):
            if not s.enabled:
                continue
            target = 480e3 if s.kind == "wfm" else 48e3
            k = max(1, int(round(fs / target)))
            paths.append(_Path(s, fs, k, seed=7 + i))
        self._paths = paths
        lcm = 1
        for p in paths:
            lcm = lcm * p.k // math.gcd(lcm, p.k)
        self._lcm = lcm
        self._block = lcm * max(1, int(math.ceil(fs / 50 / lcm)))
        self._rebuild = False

    def generate_block(self) -> tuple[np.ndarray, float, float]:
        """Produce one block (also used directly by tests, without the thread)."""
        with self._lock:
            if self._rebuild:
                self._build_paths()
            fs, fc, n = self._rate, self._center, self._block
            sigma = np.float32(10 ** (self._values.get("noise_db", -45.0) / 20.0))
            start = int(self._rng.integers(0, len(self._noise)))
            idx = (start + np.arange(n)) % len(self._noise) if start + n > len(self._noise) else slice(start, start + n)
            x = self._noise[idx] * sigma
            for p in self._paths:
                off = p.sig.frequency - fc
                if abs(off) < fs / 2 + p.BW[p.sig.kind] / 2:
                    x += p.generate(n, off)
                else:
                    p.generate_low(n // p.k)  # keep its timeline running
            if self._values.get("dc_spike"):
                x += np.complex64(0.02 + 0.015j)
            self.blocks_generated += 1
            return x, fc, fs

    def start(self, callback, error_callback=None):
        if self._running:
            return
        if not self._open:
            raise SourceError("Simulated source is not open")
        self._callback = callback
        self._error_cb = error_callback
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, name="sim-source", daemon=True)
        self._thread.start()

    def stop(self):
        if not self._running:
            return
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        self._running = False
        self._thread = None

    def _run(self):
        next_t = time.monotonic()
        try:
            while not self._stop.is_set():
                x, fc, fs = self.generate_block()
                self._callback(x, fc, fs)
                next_t += len(x) / fs
                delay = next_t - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)
                elif delay < -0.25:
                    # generation can't keep up in real time: skip ahead, count it
                    self.overflows += 1
                    next_t = time.monotonic()
        except Exception as e:  # pragma: no cover - reported to the user
            self._running = False
            self._report_error(f"Simulator failed: {e}")
            raise
