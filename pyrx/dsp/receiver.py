"""The complete receive chain for one VFO, without threads:

wideband IQ -> mixer (VFO offset) -> multistage decimator -> channel filter -> squelch gate
-> demodulator -> audio resampler (+ drift correction) -> AGC / manual gain -> audio
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .agc import Agc, Squelch
from .demod import WfmDemodulator, make_demodulator
from .filters import (FirFilter, MultiStageDecimator, choose_decimation, windowed_bandpass_complex,
                      windowed_lowpass)
from .nco import Mixer
from .resample import AudioResampler

MODES = ["NFM", "AM", "LSB", "USB", "WFM", "DSB", "CW", "RAW"]


@dataclass(frozen=True)
class ModeInfo:
    bandwidth: float          # default bandwidth, Hz
    min_bw: float
    max_bw: float
    order: int                # default channel filter taps
    presets: tuple            # bandwidth presets, Hz
    sideband: str = "both"    # both / upper / lower: how the passband sits around the VFO
    step: float = 1000.0      # default tuning step


MODE_INFO: dict[str, ModeInfo] = {
    "NFM": ModeInfo(12500, 1000, 40000, 255, (6250, 8330, 10000, 12500, 20000, 25000), step=12500),
    "AM": ModeInfo(10000, 1000, 40000, 255, (5000, 6000, 8330, 10000, 15000), step=5000),
    "LSB": ModeInfo(2700, 300, 12000, 511, (1800, 2400, 2700, 3000), "lower", step=100),
    "USB": ModeInfo(2700, 300, 12000, 511, (1800, 2400, 2700, 3000), "upper", step=100),
    "WFM": ModeInfo(200000, 30000, 300000, 127, (150000, 180000, 200000, 250000), step=100000),
    "DSB": ModeInfo(6000, 500, 20000, 255, (3000, 6000, 10000), step=1000),
    "CW": ModeInfo(500, 50, 3000, 1023, (100, 250, 500, 1000), step=10),
    "RAW": ModeInfo(40000, 2000, 40000, 255, (10000, 20000, 40000), step=1000),
}

# audio low-pass per mode (None = only anti-aliasing; the channel filter limits the band)
AUDIO_CUTOFF = {"WFM": (15000.0, 18000.0), "NFM": (5000.0, 7500.0)}


def passband(mode: str, bandwidth: float) -> tuple[float, float]:
    """Passband edges relative to the VFO, Hz."""
    side = MODE_INFO[mode].sideband
    if side == "upper":
        return 0.0, bandwidth
    if side == "lower":
        return -bandwidth, 0.0
    return -bandwidth / 2, bandwidth / 2


def min_channel_rate(mode: str, bandwidth: float) -> float:
    if mode == "WFM":
        return max(240e3, 1.2 * bandwidth)
    return max(48e3, 1.25 * bandwidth)


@dataclass
class AgcSettings:
    enabled: bool = True
    attack_ms: float = 5.0
    decay_ms: float = 250.0
    threshold_db: float = -90.0
    slope_db: float = 0.0
    hang_ms: float = 0.0
    manual_gain_db: float = 0.0


class ReceiverChain:
    def __init__(self, fs_in: float, audio_rate: float = 48000.0, mode: str = "NFM",
                 bandwidth: float | None = None, offset: float = 0.0,
                 filter_window: str = "blackmanharris", filter_order: int | None = None,
                 deemphasis_tau: float = 50e-6, stereo: bool = False, cw_pitch: float = 600.0,
                 squelch_enabled: bool = False, squelch_db: float = -60.0,
                 agc: AgcSettings | None = None):
        self.fs_in = float(fs_in)
        self.audio_rate = float(audio_rate)
        self.mode = mode.upper()
        self.bandwidth = float(bandwidth or MODE_INFO[self.mode].bandwidth)
        self.offset = float(offset)
        self.filter_window = filter_window
        self.filter_order = int(filter_order or MODE_INFO[self.mode].order)
        self.deemphasis_tau = deemphasis_tau
        self.stereo = stereo
        self.cw_pitch = cw_pitch
        self.agc_settings = agc or AgcSettings()
        self._squelch_cfg = (squelch_enabled, squelch_db)
        self._drift = 1.0
        self._build()

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        mode = self.mode
        self.bandwidth = min(max(self.bandwidth, MODE_INFO[mode].min_bw), MODE_INFO[mode].max_bw)
        d = choose_decimation(self.fs_in, min(min_channel_rate(mode, self.bandwidth), self.fs_in))
        self.fs_ch = self.fs_in / d
        protect = min(max(0.42 * self.fs_ch, self.bandwidth / 2 + 2000), 0.46 * self.fs_ch)
        self.mixer = Mixer(self.fs_in, -self.offset)
        self.decimator = MultiStageDecimator(self.fs_in, d, protect)
        self.channel = FirFilter(self._channel_taps())
        self.demod = make_demodulator(mode, self.fs_ch, tau=self.deemphasis_tau, stereo=self.stereo,
                                      cw_pitch=self.cw_pitch)
        en, thr = self._squelch_cfg
        self.squelch = Squelch(self.fs_ch, thr, en)
        cut = AUDIO_CUTOFF.get(mode)
        if cut:
            self.resampler = AudioResampler(self.fs_ch, self.audio_rate, cut[0], cut[1])
        else:
            self.resampler = AudioResampler(self.fs_ch, self.audio_rate)
        self.resampler.set_drift(self._drift)
        a = self.agc_settings
        self.agc = Agc(self.audio_rate, a.attack_ms, a.decay_ms, a.threshold_db, a.slope_db, a.hang_ms)
        self._chan_power = 0.0

    def _channel_taps(self) -> np.ndarray:
        lo, hi = passband(self.mode, self.bandwidth)
        n = self.filter_order
        if self.mode in ("USB", "LSB"):
            return windowed_bandpass_complex(n, lo, hi, self.fs_ch, self.filter_window)
        return windowed_lowpass(n, self.bandwidth / 2, self.fs_ch, self.filter_window)

    # ------------------------------------------------------------------ parameters
    def set_offset(self, offset: float) -> None:
        self.offset = float(offset)
        self.mixer.set_frequency(-self.offset)

    def set_mode(self, mode: str, bandwidth: float | None = None, filter_order: int | None = None) -> None:
        self.mode = mode.upper()
        self.bandwidth = float(bandwidth or MODE_INFO[self.mode].bandwidth)
        self.filter_order = int(filter_order or MODE_INFO[self.mode].order)
        self._build()

    def set_bandwidth(self, bandwidth: float) -> None:
        info = MODE_INFO[self.mode]
        bw = float(min(max(bandwidth, info.min_bw), info.max_bw))
        need = min(min_channel_rate(self.mode, bw), self.fs_in)
        d_new = choose_decimation(self.fs_in, need)
        self.bandwidth = bw
        if abs(self.fs_in / d_new - self.fs_ch) > 1e-6:
            self._build()
        else:
            self.channel.set_taps(self._channel_taps())

    def set_filter(self, window: str | None = None, order: int | None = None) -> None:
        if window:
            self.filter_window = window
        if order:
            self.filter_order = int(order)
        self.channel.set_taps(self._channel_taps())

    def set_deemphasis(self, tau: float) -> None:
        self.deemphasis_tau = tau
        if isinstance(self.demod, WfmDemodulator):
            self.demod.set_deemphasis(tau)

    def set_stereo(self, on: bool) -> None:
        self.stereo = bool(on)
        if isinstance(self.demod, WfmDemodulator):
            self.demod.stereo_enabled = self.stereo

    def set_cw_pitch(self, hz: float) -> None:
        self.cw_pitch = float(hz)
        if self.mode == "CW":
            self.demod.set_bfo(self.cw_pitch)

    def set_squelch(self, enabled: bool | None = None, threshold_db: float | None = None) -> None:
        en, thr = self._squelch_cfg
        en = en if enabled is None else bool(enabled)
        thr = thr if threshold_db is None else float(threshold_db)
        self._squelch_cfg = (en, thr)
        self.squelch.enabled = en
        self.squelch.threshold_db = thr

    def set_agc(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self.agc_settings, k, v)
        a = self.agc_settings
        self.agc.configure(a.attack_ms, a.decay_ms, a.threshold_db, a.slope_db, a.hang_ms)

    def set_drift(self, ratio: float) -> None:
        self._drift = float(ratio)
        self.resampler.set_drift(self._drift)

    # ------------------------------------------------------------------ status
    @property
    def channel_level_db(self) -> float:
        return self.squelch.level_db

    @property
    def squelch_open(self) -> bool:
        return self.squelch.open

    @property
    def stereo_detected(self) -> bool:
        return isinstance(self.demod, WfmDemodulator) and self.demod.pilot_detected

    @property
    def channels(self) -> int:
        return 2 if self.demod.stereo_output else 1

    # ------------------------------------------------------------------ processing
    def process(self, iq: np.ndarray) -> np.ndarray:
        if abs(self.offset) > self.fs_in / 2:
            # VFO outside the captured band: keep the time base, output silence
            n_out = int(round(len(iq) / self.fs_in * self.audio_rate))
            return np.zeros((n_out, 2) if self.channels == 2 else n_out, np.float32)
        x = self.mixer.process(iq)
        x = self.decimator.process(x)
        x = self.channel.process(x)
        gate = self.squelch.gains(x)
        audio = self.demod.process(x)
        if self.squelch.enabled or gate.min() < 1.0:
            audio = audio * (gate[:, None] if audio.ndim == 2 else gate)
        audio = self.resampler.process(audio)
        if self.agc_settings.enabled:
            audio = self.agc.process(audio)
        elif self.agc_settings.manual_gain_db:
            audio = audio * np.float32(10 ** (self.agc_settings.manual_gain_db / 20))
        return audio.astype(np.float32, copy=False)
