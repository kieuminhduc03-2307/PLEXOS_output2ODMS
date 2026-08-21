from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


SOURCE_TIME_BASES = {"unknown_local", "utc", "iana_timezone"}


@dataclass(frozen=True)
class SourceTimeContext:
    source_wall_clock: datetime
    source_time_basis: str = "unknown_local"
    source_timezone: str | None = None
    analysis_timezone: str | None = None

    def __post_init__(self) -> None:
        if self.source_wall_clock.tzinfo is not None:
            raise ValueError("source_wall_clock must be timezone-naive")
        if self.source_time_basis not in SOURCE_TIME_BASES:
            raise ValueError(f"Unsupported source_time_basis: {self.source_time_basis}")
        if self.source_time_basis == "iana_timezone" and not self.source_timezone:
            raise ValueError("source_timezone is required for iana_timezone source time")
        if self.source_time_basis != "iana_timezone" and self.source_timezone:
            raise ValueError("source_timezone is allowed only for iana_timezone source time")
        if self.source_timezone:
            ZoneInfo(self.source_timezone)
        if self.analysis_timezone:
            ZoneInfo(self.analysis_timezone)

    @property
    def source_aware(self) -> datetime | None:
        if self.source_time_basis == "utc":
            return self.source_wall_clock.replace(tzinfo=timezone.utc)
        if self.source_time_basis == "iana_timezone":
            return self.source_wall_clock.replace(tzinfo=ZoneInfo(self.source_timezone or ""))
        return None

    @property
    def analysis_aware(self) -> datetime | None:
        if self.analysis_timezone:
            return self.source_wall_clock.replace(tzinfo=ZoneInfo(self.analysis_timezone))
        return self.source_aware

    def to_dict(self) -> dict:
        source_aware = self.source_aware
        analysis_aware = self.analysis_aware
        return {
            "source_wall_clock": self.source_wall_clock.isoformat(),
            "source_time_basis": self.source_time_basis,
            "source_timezone": self.source_timezone,
            "analysis_timezone": self.analysis_timezone,
            "analysis_timestamp": analysis_aware.isoformat() if analysis_aware else None,
            "timestamp_utc": (
                source_aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if source_aware
                else None
            ),
            "analysis_timestamp_utc": (
                analysis_aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if analysis_aware
                else None
            ),
        }


def parse_source_wall_clock(value: str) -> datetime:
    result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if result.tzinfo is not None:
        raise ValueError(
            "Source wall clock must not contain an offset; declare --source-time-basis instead"
        )
    return result
