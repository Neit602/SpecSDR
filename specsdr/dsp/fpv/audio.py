"""FM audio subcarrier of an FPV video signal (no Qt): discriminator output -> mixer to the
subcarrier -> decimation -> second FM discriminator -> de-emphasis -> resampling to audio rate."""

from __future__ import annotations

import numpy as np

from ..demod import FmDiscriminator
from ..filters import MultiStageDecimator, choose_decimation, deemphasis
from ..nco import Mixer
from ..resample import AudioResampler

SUBCARRIERS = (6.5e6, 6.0e6, 5.5e6)
DEVIATION = 50e3               # audio subcarrier deviation, Hz (FM TV audio convention)


class FpvAudio:
    """Takes the discriminator output (real, Hz) and produces float32 mono audio blocks."""

    def __init__(self, fs: float, audio_rate: float = 48000.0, subcarrier: float = 6.5e6):
        self.fs = float(fs)
        self.audio_rate = float(audio_rate)
        self.subcarrier = float(subcarrier)
        self.enabled = False
        self.level = 0.0
        self._build()

    def _build(self) -> None:
        self._mix = Mixer(self.fs, -self.subcarrier)
        d = choose_decimation(self.fs, 160e3)
        self.fs_if = self.fs / d
        self._dec = MultiStageDecimator(self.fs, d, 55e3, atten_db=70.0)
        self._disc = FmDiscriminator(self.fs_if, DEVIATION)
        self._de = deemphasis(self.fs_if, 50e-6)
        self.resampler = AudioResampler(self.fs_if, self.audio_rate, 15000.0, 17000.0)
        self.output: list[np.ndarray] = []

    def set_subcarrier(self, hz: float) -> None:
        if hz != self.subcarrier:
            self.subcarrier = float(hz)
            self._mix.set_frequency(-self.subcarrier)

    def set_drift(self, ratio: float) -> None:
        self.resampler.set_drift(ratio)

    def process(self, d: np.ndarray) -> None:
        """One chunk of discriminator output; audio is appended to self.output."""
        if not self.enabled:
            return
        z = self._mix.process(d.astype(np.complex64))
        z = self._dec.process(z)
        a = self._disc.process(z)
        a = self._de.process(a)
        a = self.resampler.process(a)
        self.level += 0.05 * (float(np.abs(a).mean()) - self.level)
        self.output.append(a.astype(np.float32))

    def take(self) -> np.ndarray:
        out = self.output
        self.output = []
        return np.concatenate(out) if out else np.zeros(0, np.float32)
