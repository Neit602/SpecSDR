"""Analog video standards: everything the encoder (simulator) and the decoder need to agree
on. No Qt. Times are microseconds from 0H (the half-amplitude point of the leading sync edge)."""

from __future__ import annotations

from dataclasses import dataclass

PIXELS = 720            # horizontal pixel grid (one BT.601 line)
PIXEL_US = 720 / 13.5 / 720   # 13.5 MHz pixel clock: 1/13.5 us per pixel


@dataclass(frozen=True)
class VideoStandard:
    name: str
    lines: int                  # lines per frame
    field_rate: float           # fields per second
    line_freq: float            # Hz
    fsc: float                  # colour subcarrier, Hz
    active_lines: int           # picture lines per field
    # first picture line relative to the line in which the broad (vertical) pulses begin,
    # for a vsync starting at the line start (field 1) and at mid-line (field 2)
    first_active: tuple[int, int]
    sync_us: float              # horizontal sync width
    eq_us: float                # equalising pulse width
    burst_start_us: float
    burst_cycles: float
    active_start_us: float
    active_us: float
    clamp_us: tuple[float, float]  # back-porch window used for the black-level clamp
    sync_v: float               # sync tip level, volts (blanking = 0 V)
    setup_v: float              # black level above blanking
    white_v: float              # peak white
    burst_v: float              # burst amplitude (peak), volts
    chroma_v: float             # volts per unit of U/V
    pal: bool

    @property
    def line_us(self) -> float:
        return 1e6 / self.line_freq

    @property
    def field_halves(self) -> int:
        """Half-lines per field: the number that tells PAL from NTSC."""
        return self.lines

    @property
    def frame_rate(self) -> float:
        return self.field_rate / 2.0

    @property
    def fields_per_second(self) -> float:
        return self.field_rate

    @property
    def grid_start_us(self) -> float:
        """Time of pixel 0: the 720-pixel grid is centred on the active line."""
        return self.active_start_us + self.active_us / 2 - PIXELS * PIXEL_US / 2

    @property
    def rows(self) -> int:
        return self.active_lines * 2

    def volts_to_y(self, volts: float) -> float:
        return (volts - self.setup_v) / (self.white_v - self.setup_v)


PAL = VideoStandard(
    name="PAL", lines=625, field_rate=50.0, line_freq=15625.0, fsc=4433618.75, active_lines=288,
    first_active=(22, 23), sync_us=4.7, eq_us=2.35, burst_start_us=5.6, burst_cycles=10.0,
    active_start_us=10.5, active_us=52.0, clamp_us=(8.3, 10.0), sync_v=-0.3, setup_v=0.0,
    white_v=0.7, burst_v=0.15, chroma_v=0.7, pal=True)

NTSC = VideoStandard(
    name="NTSC", lines=525, field_rate=60000.0 / 1001.0, line_freq=4.5e6 / 286.0,
    fsc=315e6 / 88.0, active_lines=240, first_active=(19, 20), sync_us=4.7, eq_us=2.3,
    burst_start_us=5.3, burst_cycles=9.0, active_start_us=9.4, active_us=52.656, clamp_us=(8.0, 8.9),
    sync_v=-40.0 / 140.0, setup_v=7.5 / 140.0, white_v=100.0 / 140.0, burst_v=20.0 / 140.0,
    chroma_v=92.5 / 140.0, pal=False)

STANDARDS = {"PAL": PAL, "NTSC": NTSC}
