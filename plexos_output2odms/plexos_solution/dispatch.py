from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SolutionSelection:
    phase: str
    period: str
    timestamp: datetime
    sample: str
    unit: str | None = None


@dataclass(frozen=True)
class DispatchRecord:
    timestamp: datetime
    generator_name: str
    generation_mw: float
    source_unit: str
    phase: str
    period: str
    sample: str
    source_object_id: int | None = None
