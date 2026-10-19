"""Tuning policy (pure functions, unit-tested): where the hardware centre frequency must
go when the VFO moves, LO-offset mode, and step snapping."""

from __future__ import annotations

from dataclasses import dataclass

STEPS = [1, 10, 50, 100, 500, 1000, 2500, 5000, 6250, 8333.333333, 9000, 10000, 12500, 25000,
         50000, 100000, 200000, 1000000]


def snap(hz: float, step: float) -> float:
    if step <= 0:
        return hz
    return round(round(hz / step) * step, 3)


@dataclass
class TuneContext:
    center: float
    sample_rate: float
    usable_bw: float              # alias-free part of the span
    pb_lo: float                  # passband edges relative to the VFO
    pb_hi: float
    tunable: bool = True
    freq_min: float = 0.0
    freq_max: float = 6e9
    lo_offset: bool = False       # keep the VFO away from DC
    lo_offset_hz: float = 250e3


def plan(vfo: float, ctx: TuneContext) -> tuple[float, float]:
    """Return (vfo, centre) after applying the policy:

    * the passband must stay inside the usable bandwidth; if it would leave it, the centre
      follows (recentred on the VFO, or offset from it in LO-offset mode);
    * in LO-offset mode the VFO's passband never covers DC (plus a guard);
    * a non-tunable source (file) never moves its centre: the VFO is clamped instead;
    * the centre stays within the device range."""
    half = ctx.usable_bw / 2
    if not ctx.tunable:
        lo = ctx.center - ctx.sample_rate / 2 - ctx.pb_lo
        hi = ctx.center + ctx.sample_rate / 2 - ctx.pb_hi
        return min(max(vfo, lo), hi), ctx.center

    vfo = min(max(vfo, ctx.freq_min), ctx.freq_max)
    center = ctx.center
    guard = max(abs(ctx.pb_lo), abs(ctx.pb_hi)) + 5e3
    offset = min(max(ctx.lo_offset_hz, guard * 1.5), max(half - max(abs(ctx.pb_lo), abs(ctx.pb_hi)), 0))

    def outside(c):
        return vfo + ctx.pb_lo < c - half or vfo + ctx.pb_hi > c + half

    def on_dc(c):
        return ctx.lo_offset and (vfo + ctx.pb_lo - guard / 2 < c < vfo + ctx.pb_hi + guard / 2)

    if outside(center) or on_dc(center):
        center = vfo - offset if ctx.lo_offset else vfo
    center = min(max(center, ctx.freq_min), ctx.freq_max)
    return vfo, center
