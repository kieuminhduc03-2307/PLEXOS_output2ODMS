from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from ..crosswalk.load_snapshot import LoadMapping


def read_rts_regional_load(path: str | Path, timestamp: datetime) -> dict[str, float]:
    """Read one RTS day-ahead row; Period 1 is the interval beginning at 00:00."""
    if timestamp.tzinfo is not None:
        raise ValueError("Regional-load selection must use the timezone-naive source wall clock")
    expected_period = timestamp.hour + 1
    matches: list[dict[str, str]] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if (
                int(row["Year"]) == timestamp.year
                and int(row["Month"]) == timestamp.month
                and int(row["Day"]) == timestamp.day
                and int(row["Period"]) == expected_period
            ):
                matches.append(row)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one RTS regional load row for {timestamp.isoformat()} "
            f"(Period {expected_period}), found {len(matches)}"
        )
    row = matches[0]
    regions = {name.strip(): float(value) for name, value in row.items() if name.strip().isdigit()}
    if not regions or any(value < 0 for value in regions.values()):
        raise ValueError("RTS regional load row is empty or contains negative demand")
    return regions


def allocate_rts_nodal_load(
    regional_mw: dict[str, float], mappings: list[LoadMapping]
) -> list[dict]:
    unapproved = sorted(item.source_bus_id for item in mappings if not item.approved)
    if unapproved:
        raise ValueError(f"Load mappings must be approved: {unapproved[:20]}")
    base_by_region: dict[str, float] = {}
    for item in mappings:
        base_by_region[item.source_area] = base_by_region.get(item.source_area, 0.0) + item.source_base_p_mw
    if set(base_by_region) != set(regional_mw):
        raise ValueError(
            f"Regional load areas do not match crosswalk: input={sorted(regional_mw)} "
            f"crosswalk={sorted(base_by_region)}"
        )
    rows: list[dict] = []
    for item in mappings:
        base_total = base_by_region[item.source_area]
        if base_total <= 0:
            raise ValueError(f"Region {item.source_area} has non-positive base load")
        scale = regional_mw[item.source_area] / base_total
        rows.append(
            {
                "resource_type": "LOAD",
                "source_load_id": item.identity,
                "source_bus_id": item.source_bus_id,
                "source_region": item.source_area,
                "regional_load_mw": regional_mw[item.source_area],
                "regional_scale": scale,
                "base_p_mw": item.source_base_p_mw,
                "base_q_mvar": item.source_base_q_mvar,
                "load_p_mw": item.source_base_p_mw * scale,
                "load_q_mvar": item.source_base_q_mvar * scale,
                "p_provenance": "RTS_AUTHORITATIVE_DAY_AHEAD_REGIONAL_LOAD",
                "q_provenance": "RTS_DERIVED_AC_EMBEDDING",
                "q_policy": "preserve_base_pf",
                "target_load_name": item.odms_load_name,
                "target_load_mrid": item.odms_load_mrid,
            }
        )
    return rows
