from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path


FULL_STATUS_CLASSES = {"CT", "CC", "STEAM", "NUCLEAR", "HYDRO"}
ON_ONLY_CLASSES = {"PV", "WIND", "RTPV"}


def _resource_class(name: str) -> str:
    if "_SYNC_COND_" in name:
        return "SYNC_COND"
    parts = name.split("_")
    return parts[1].upper() if len(parts) >= 3 else "UNKNOWN"


def read_wide_commitment(path: str | Path, timestamp: datetime) -> dict[str, float]:
    if timestamp.tzinfo is None:
        raise ValueError("Timestamp must include an explicit timezone")
    matches: list[dict[str, str]] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            value = datetime.fromisoformat(row["time"].strip())
            if value.replace(tzinfo=timestamp.tzinfo) == timestamp:
                matches.append(row)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one commitment row for {timestamp.isoformat()}, found {len(matches)}"
        )
    result: dict[str, float] = {}
    for name, raw_value in matches[0].items():
        if name == "time":
            continue
        value = float(raw_value)
        if value not in (0.0, 1.0):
            raise ValueError(
                f"V1 commitment requires 1:1 binary Units Generating; {name} has {value}"
            )
        result[name] = value
    return result


def build_class_aware_statuses(
    commitment: dict[str, float], generator_rows: list[dict]
) -> list[dict]:
    targets = {row["source_generator"]: row for row in generator_rows}
    if set(commitment) != set(targets):
        missing = sorted(set(targets) - set(commitment))
        extra = sorted(set(commitment) - set(targets))
        raise ValueError(f"Commitment/dispatch identity mismatch: missing={missing[:20]} extra={extra[:20]}")
    result: list[dict] = []
    for source_name, value in sorted(commitment.items()):
        target = targets[source_name]
        resource_class = _resource_class(source_name)
        if resource_class in FULL_STATUS_CLASSES:
            action = "set"
            requested = bool(value)
            policy = "authoritative_binary_commitment"
        elif resource_class in ON_ONLY_CLASSES and value == 1.0:
            action = "set"
            requested = True
            policy = "authoritative_commitment_on_only"
        else:
            action = "preserve"
            requested = None
            policy = (
                "preserve_synchronous_condenser"
                if resource_class == "SYNC_COND"
                else "preserve_zero_variable_resource"
                if resource_class in ON_ONLY_CLASSES
                else "preserve_uncommissioned_resource_class"
            )
        result.append(
            {
                "resource_type": "UNIT_STATUS",
                "timestamp": target["timestamp"],
                "source_generator": source_name,
                "source_units_generating": value,
                "resource_class": resource_class,
                "action": action,
                "requested_in_service": requested,
                "policy": policy,
                "provenance": "PLEXOS_UNITS_GENERATING",
                "target_machine_name": target["target_machine_name"],
                "target_machine_mrid": target["target_machine_mrid"],
            }
        )
    return result
