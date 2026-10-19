"""Source registry. Hardware modules import their driver libraries lazily, so a missing
library never prevents the application from starting."""

from __future__ import annotations

from .base import Control, DeviceInfo, Source, SourceError
from .iqfile import IqFileSource
from .simulated import SimulatedSource


def source_classes() -> list[type[Source]]:
    from .hackrf_source import HackRFSource
    from .rtlsdr_source import RtlSdrSource
    return [SimulatedSource, IqFileSource, RtlSdrSource, HackRFSource]


def source_class(name: str) -> type[Source]:
    for cls in source_classes():
        if cls.name == name:
            return cls
    raise KeyError(name)


__all__ = ["Control", "DeviceInfo", "Source", "SourceError", "SimulatedSource", "IqFileSource",
           "source_classes", "source_class"]
