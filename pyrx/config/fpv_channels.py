"""FPV video channels. The standard 5.8 GHz table is built in; user-defined groups (1.3 GHz, 3.3
GHz, non-standard frequencies) live in the frequency manager (bookmarks.json) as FPV-mode
bookmarks grouped by name, so channels and bookmarks are one list."""

from __future__ import annotations

from .bookmarks import Bookmark, BookmarkStore

# MHz. A = Boscam A / B = Boscam B / E = Boscam E / F = Fatshark / R = Raceband / L = Low band
BANDS = {
    "A": (5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725),
    "B": (5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866),
    "E": (5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945),
    "F": (5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880),
    "R": (5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917),
    "L": (5362, 5399, 5436, 5473, 5510, 5547, 5584, 5621),
}
FPV_BW = 18e6             # displayed passband, Hz


def channel_name(band: str, n: int) -> str:
    return f"{band}{n + 1}"


def standard_channels(bands=None) -> list[tuple[str, float]]:
    """[(name, Hz)] of the built-in table, restricted to `bands` if given."""
    out = []
    for b, table in BANDS.items():
        if bands is not None and b not in bands:
            continue
        out.extend((channel_name(b, i), f * 1e6) for i, f in enumerate(table))
    return out


def user_groups(store: BookmarkStore) -> list[str]:
    """Group names holding FPV channels in the frequency manager."""
    return sorted({b.group for b in store.items if b.mode == "FPV"})


def group_channels(store: BookmarkStore, group: str) -> list[tuple[str, float]]:
    return [(b.name, float(b.frequency)) for b in store.items if b.mode == "FPV" and b.group == group]


def nearest_channel(freq: float, store: BookmarkStore | None = None, max_distance: float = 10e6) -> str:
    """Name of the nearest known channel within max_distance, or ''."""
    cands = standard_channels()
    if store is not None:
        cands = cands + [(b.name, float(b.frequency)) for b in store.items if b.mode == "FPV"]
    if not cands:
        return ""
    name, f = min(cands, key=lambda c: abs(c[1] - freq))
    return name if abs(f - freq) <= max_distance else ""


def make_bookmark(name: str, freq: float, group: str) -> Bookmark:
    return Bookmark(name, freq, "FPV", FPV_BW, group or "FPV")
