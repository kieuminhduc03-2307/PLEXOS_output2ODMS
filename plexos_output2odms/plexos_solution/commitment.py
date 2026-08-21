from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .dispatch import SolutionSelection
from .reader import _read_native_generator_property


def read_wide_commitment(path: str | Path, timestamp: datetime) -> dict[str, float]:
    if timestamp.tzinfo is not None:
        raise ValueError("Commitment selection must use the timezone-naive source wall clock")
    matches: list[dict[str, str]] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            value = datetime.fromisoformat(row["time"].strip())
            if value.tzinfo is not None:
                raise ValueError("Commitment source unexpectedly contains timezone metadata")
            if value == timestamp:
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


def read_commitment(
    path: str | Path,
    timestamp: datetime,
    *,
    phase: str = "ST",
    period: str = "Interval",
    sample: str = "Mean",
) -> dict[str, float]:
    source = Path(path)
    if source.suffix.casefold() != ".zip":
        return read_wide_commitment(source, timestamp)
    selection = SolutionSelection(phase, period, timestamp, sample, None)
    rows = _read_native_generator_property(source, selection, "Units Generating")
    result = {}
    for row in rows:
        value = float(row["value"])
        if row["unit"] not in {"", "-"}:
            raise ValueError(
                f"Native Units Generating for {row['object_name']} has unexpected unit {row['unit']!r}"
            )
        if value not in (0.0, 1.0):
            raise ValueError(
                "V1 commitment requires 1:1 binary Units Generating; "
                f"{row['object_name']} has {value}"
            )
        result[row["object_name"]] = value
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
        resource_class = target.get("source_operating_class", "")
        status_policy = target.get("status_policy", "")
        if not resource_class or not status_policy:
            raise ValueError(
                f"Approved crosswalk metadata is missing operating class/status policy for {source_name}"
            )
        if status_policy == "BINARY_COMMITMENT":
            action = "set"
            requested = bool(value)
            policy = "authoritative_binary_commitment"
        elif status_policy == "COMMITMENT_ON_ONLY" and value == 1.0:
            action = "set"
            requested = True
            policy = "authoritative_commitment_on_only"
        elif status_policy in {"PRESERVE", "COMMITMENT_ON_ONLY"}:
            action = "preserve"
            requested = None
            policy = (
                "preserve_synchronous_condenser"
                if resource_class == "SYNCHRONOUS_CONDENSER"
                else "preserve_zero_variable_resource"
                if status_policy == "COMMITMENT_ON_ONLY"
                else "preserve_uncommissioned_resource_class"
            )
        else:
            raise ValueError(f"Unsupported approved status policy {status_policy!r} for {source_name}")
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
