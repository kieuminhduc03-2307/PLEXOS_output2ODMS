from __future__ import annotations

import csv
import math
from datetime import datetime
from pathlib import Path

from ..crosswalk.load_snapshot import LoadMapping


REQUIRED_FIELDS = {
    "timestamp", "source_load_id", "p_mw", "q_mvar",
    "p_provenance", "q_provenance", "q_policy",
}


def read_normalized_load_series(
    path: str | Path,
    timestamp: datetime,
    mappings: list[LoadMapping],
    *,
    missing_policy: str = "error",
) -> list[dict]:
    """Select and validate one exact, timezone-naive normalized load snapshot."""
    if timestamp.tzinfo is not None:
        raise ValueError("Load-series selection requires a timezone-naive source wall clock")
    if missing_policy not in {"error", "preserve"}:
        raise ValueError(f"Unsupported missing-load policy: {missing_policy}")
    unapproved = sorted(item.identity for item in mappings if not item.approved)
    if unapproved:
        raise ValueError(f"Load mappings must be approved: {unapproved[:20]}")
    by_source = {item.identity: item for item in mappings}
    if len(by_source) != len(mappings):
        raise ValueError("Load crosswalk contains duplicate source_load_id values")
    targets = [item.odms_load_mrid for item in mappings]
    if len(targets) != len(set(targets)):
        raise ValueError("Load crosswalk contains duplicate ODMS targets")

    selected: list[dict] = []
    seen: set[str] = set()
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing_columns = REQUIRED_FIELDS - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(f"Load series missing columns: {sorted(missing_columns)}")
        for line, raw in enumerate(reader, 2):
            try:
                row_time = datetime.fromisoformat(raw["timestamp"])
            except ValueError as exc:
                raise ValueError(f"Invalid load timestamp at line {line}") from exc
            if row_time.tzinfo is not None:
                raise ValueError(f"Load timestamp must be timezone-naive at line {line}")
            if row_time != timestamp:
                continue
            source_id = raw["source_load_id"].strip()
            if source_id in seen:
                raise ValueError(f"Duplicate load row for {source_id} at {timestamp.isoformat()}")
            seen.add(source_id)
            mapping = by_source.get(source_id)
            if mapping is None:
                raise ValueError(f"Unapproved/unmapped source_load_id: {source_id}")
            p_mw, q_mvar = float(raw["p_mw"]), float(raw["q_mvar"])
            if not math.isfinite(p_mw) or not math.isfinite(q_mvar):
                raise ValueError(f"Non-finite load value for {source_id}")
            provenance = [raw[name].strip() for name in ("p_provenance", "q_provenance", "q_policy")]
            if not all(provenance):
                raise ValueError(f"Load provenance/policy is empty for {source_id}")
            selected.append({
                "resource_type": "LOAD",
                "source_load_id": source_id,
                "source_bus_id": mapping.source_bus_id,
                "source_region": mapping.source_area,
                "base_p_mw": mapping.source_base_p_mw,
                "base_q_mvar": mapping.source_base_q_mvar,
                "load_p_mw": p_mw,
                "load_q_mvar": q_mvar,
                "p_provenance": provenance[0],
                "q_provenance": provenance[1],
                "q_policy": provenance[2],
                "target_load_name": mapping.odms_load_name,
                "target_load_mrid": mapping.odms_load_mrid,
            })
    missing = sorted(set(by_source) - seen)
    if missing and missing_policy != "preserve":
        raise ValueError(f"Load series missing mapped identities: {missing[:20]}")
    if not selected:
        raise ValueError(f"No load rows for exact timestamp {timestamp.isoformat()}")
    return selected
