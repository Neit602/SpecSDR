"""Frequency manager storage: bookmarks in a JSON file."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field

from .settings import config_dir

log = logging.getLogger(__name__)


@dataclass
class Bookmark:
    name: str
    frequency: float
    mode: str
    bandwidth: float
    group: str = "General"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


class BookmarkStore:
    FILE = "bookmarks.json"

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(config_dir(), self.FILE)
        self.items: list[Bookmark] = []

    def load(self) -> "BookmarkStore":
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("bookmarks", []) if isinstance(raw, dict) else raw
            self.items = []
            for d in items:
                try:
                    self.items.append(Bookmark(name=str(d["name"]), frequency=float(d["frequency"]),
                                               mode=str(d.get("mode", "NFM")), bandwidth=float(d.get("bandwidth", 0)),
                                               group=str(d.get("group", "General")) or "General",
                                               id=str(d.get("id") or uuid.uuid4().hex[:12])))
                except (KeyError, TypeError, ValueError) as e:
                    log.warning("skipping bad bookmark %r: %s", d, e)
        except FileNotFoundError:
            self.items = []
        except (OSError, ValueError) as e:
            log.warning("cannot read bookmarks %s: %s", self.path, e)
        return self

    def save(self) -> None:
        d = os.path.dirname(self.path)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".bookmarks", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "bookmarks": [asdict(b) for b in self.items]}, f, indent=2)
        os.replace(tmp, self.path)

    def groups(self) -> list[str]:
        return sorted({b.group for b in self.items})

    def add(self, b: Bookmark) -> Bookmark:
        self.items.append(b)
        return b

    def remove(self, bid: str) -> None:
        self.items = [b for b in self.items if b.id != bid]

    def get(self, bid: str) -> Bookmark | None:
        return next((b for b in self.items if b.id == bid), None)
