from __future__ import annotations

import csv
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .crosswalk.generator_dispatch import GeneratorMapping, load_crosswalk
from .odms.ssh import write_ssh
from .plexos_solution.dispatch import SolutionSelection
from .plexos_solution.reader import read_dispatch
from .validation import ValidationReport


@dataclass(frozen=True)
class SnapshotConfig:
    phase: str = "ST"
    period: str = "Interval"
    sample: str = "Mean"
    unit: str | None = None
    bounds_tolerance_mw: float = 1e-6
    missing_dispatch_policy: str = "error"


@dataclass
class SnapshotResult:
    rows: list[dict]
    report: ValidationReport
    audit: dict

    def write_normalized_csv(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "timestamp",
            "phase",
            "period",
            "sample",
            "source_generator",
            "source_guid",
            "generation_mw",
            "target_machine_name",
            "target_machine_mrid",
            "scheduled_mw",
            "cim_rotating_machine_p_mw",
        ]
        with target.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in self.rows:
                writer.writerow({name: row[name] for name in fields})

    def write_audit(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.audit, indent=2) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _default_dependency(target_cim: Path) -> str:
    return "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"odms-cim-sha256:{_sha256(target_cim)}"))


def build_dispatch_snapshot(
    solution_path: str | Path,
    crosswalk_path: str | Path,
    target_cim_path: str | Path,
    *,
    timestamp: datetime,
    config: SnapshotConfig | None = None,
    dependent_on: str | None = None,
) -> SnapshotResult:
    config = config or SnapshotConfig()
    if timestamp.tzinfo is None:
        raise ValueError("Timestamp must include an explicit timezone")
    solution = Path(solution_path)
    crosswalk_file = Path(crosswalk_path)
    target_cim = Path(target_cim_path)
    selection = SolutionSelection(
        config.phase, config.period, timestamp, config.sample, config.unit
    )
    dispatch = read_dispatch(solution, selection)
    mappings = load_crosswalk(crosswalk_file)
    report = ValidationReport()
    by_name: dict[str, GeneratorMapping] = {item.source_name: item for item in mappings}
    dispatch_names = {item.generator_name for item in dispatch}
    mapping_names = set(by_name)
    missing_mapping = sorted(dispatch_names - mapping_names)
    missing_dispatch = sorted(mapping_names - dispatch_names)
    if missing_mapping:
        report.error(
            "DISPATCH_GENERATOR_UNMAPPED",
            f"Dispatch generators absent from crosswalk: {missing_mapping[:20]}",
        )
    if missing_dispatch:
        message = f"Crosswalk generators absent at selected timestamp: {missing_dispatch[:20]}"
        if config.missing_dispatch_policy == "preserve":
            report.warning(
                "CROSSWALK_GENERATOR_PRESERVED",
                message + "; their existing ODMS ScheduledMW is intentionally preserved.",
            )
        else:
            report.error("CROSSWALK_GENERATOR_MISSING_DISPATCH", message)
    unapproved = sorted(item.source_name for item in mappings if not item.approved)
    if unapproved:
        report.error(
            "CROSSWALK_NOT_APPROVED",
            f"Mappings must be explicitly approved before snapshot generation: {unapproved[:20]}",
        )

    rows: list[dict] = []
    for record in dispatch:
        mapping = by_name.get(record.generator_name)
        if mapping is None or not mapping.approved:
            continue
        value = record.generation_mw
        tolerance = config.bounds_tolerance_mw
        if value < -tolerance:
            report.error(
                "DISPATCH_NEGATIVE_GENERATION",
                f"Generation is {value} MW; V1 does not infer pumping/charging semantics",
                record.generator_name,
            )
        if mapping.max_operating_p_mw is not None and value > mapping.max_operating_p_mw + tolerance:
            report.error(
                "DISPATCH_ABOVE_MAX",
                f"{value} MW exceeds ODMS maxOperatingP {mapping.max_operating_p_mw} MW",
                record.generator_name,
            )
        if (
            mapping.min_operating_p_mw is not None
            and value > tolerance
            and value < mapping.min_operating_p_mw - tolerance
        ):
            report.error(
                "DISPATCH_BELOW_MIN",
                f"{value} MW is below ODMS minOperatingP {mapping.min_operating_p_mw} MW",
                record.generator_name,
            )
        rows.append(
            {
                "timestamp": record.timestamp.isoformat(),
                "phase": record.phase,
                "period": record.period,
                "sample": record.sample,
                "source_generator": record.generator_name,
                "source_guid": mapping.source_guid,
                "source_object_id": mapping.source_object_id,
                "source_psse_key": mapping.source_psse_key,
                "generation_mw": value,
                "target_machine_name": mapping.odms_machine_name,
                "target_machine_mrid": mapping.odms_synchronous_machine_mrid,
                "target_generating_unit_mrid": mapping.odms_generating_unit_mrid,
                "scheduled_mw": value,
                "cim_p_mw": -value,
                "cim_rotating_machine_p_mw": -value,
            }
        )
    targets = [row["target_machine_mrid"] for row in rows]
    if len(targets) != len(set(targets)):
        report.error("DUPLICATE_ODMS_TARGET", "Multiple dispatch rows target one ODMS machine")
    report.info(
        "ODMS_OPERATIONAL_MAPPING",
        "PLEXOS Generation is applied as pssoPy.Unit.ScheduledMW; CIM SSH RotatingMachine.p is a load-sign representation of the same input.",
    )
    report.info(
        "COMMITMENT_POLICY",
        "V1 does not infer unit on/off status from zero Generation.",
    )
    dependency = dependent_on or _default_dependency(target_cim)
    if dependent_on is None:
        report.warning(
            "SSH_DEPENDENCY_DERIVED",
            "No authoritative EQ FullModel dependency was supplied; a deterministic target-CIM dependency URN is used for dry-run evidence only.",
        )
    audit = {
        "schema": "plexos-output2odms-audit-v1",
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "selection": {
            "phase": config.phase,
            "period": config.period,
            "sample": config.sample,
            "timestamp": timestamp.isoformat(),
            "timestamp_utc": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "sources": {
            "solution": {"path": str(solution.resolve()), "sha256": _sha256(solution)},
            "crosswalk": {"path": str(crosswalk_file.resolve()), "sha256": _sha256(crosswalk_file)},
            "target_cim": {"path": str(target_cim.resolve()), "sha256": _sha256(target_cim)},
        },
        "mapping": {
            "dispatch_rows": len(dispatch),
            "mapped_rows": len(rows),
            "approved_crosswalk_rows": sum(item.approved for item in mappings),
            "preserved_missing_dispatch": missing_dispatch,
            "scheduled_mw_total": sum(row["scheduled_mw"] for row in rows),
            "ssh_dependency": dependency,
        },
        "validation": report.to_dict(),
    }
    audit["ssh_dependency"] = dependency
    return SnapshotResult(sorted(rows, key=lambda item: item["source_generator"]), report, audit)


def write_snapshot_outputs(
    result: SnapshotResult,
    output_directory: str | Path,
    *,
    scenario_time: datetime,
) -> dict[str, str]:
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    validation = output / "dispatch.validation.json"
    audit = output / "dispatch.audit.json"
    normalized = output / "dispatch.normalized.csv"
    ssh = output / "PLEXOS_DISPATCH_SSH.xml"
    result.report.write_json(validation)
    result.write_audit(audit)
    if not result.report.ok:
        return {"validation": str(validation), "audit": str(audit)}
    result.write_normalized_csv(normalized)
    ssh_hash = write_ssh(
        result.rows,
        ssh,
        scenario_time=scenario_time,
        dependent_on=result.audit["ssh_dependency"],
    )
    result.audit["outputs"] = {
        "normalized_csv": str(normalized.resolve()),
        "ssh": str(ssh.resolve()),
        "ssh_sha256": ssh_hash,
    }
    result.write_audit(audit)
    return {
        "normalized": str(normalized),
        "ssh": str(ssh),
        "validation": str(validation),
        "audit": str(audit),
    }
