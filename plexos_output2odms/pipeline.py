from __future__ import annotations

import csv
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .crosswalk.generator_dispatch import (
    GeneratorMapping,
    list_odms_synchronous_machines,
    load_crosswalk,
)
from .crosswalk.load_snapshot import load_load_crosswalk
from .crosswalk.branch_ratings import load_branch_rating_crosswalk
from .odms.ssh import write_ssh
from .plexos_solution.dispatch import SolutionSelection
from .plexos_solution.commitment import build_class_aware_statuses, read_commitment
from .plexos_solution.reader import read_dispatch
from .plexos_solution.regional_load import allocate_rts_nodal_load, read_rts_regional_load
from .time_semantics import SourceTimeContext
from .validation import ValidationReport


@dataclass(frozen=True)
class SnapshotConfig:
    phase: str = "ST"
    period: str = "Interval"
    sample: str = "Mean"
    unit: str | None = None
    bounds_tolerance_mw: float = 1e-6
    missing_dispatch_policy: str = "error"
    preflight_balance_tolerance_mw: float = 1e-6
    source_time_basis: str = "unknown_local"
    source_timezone: str | None = None
    analysis_timezone: str | None = None
    status_mode: str = "crosswalk_commitment"
    q_limit_policy: str = "validate_only"


@dataclass
class SnapshotResult:
    rows: list[dict]
    load_rows: list[dict]
    status_rows: list[dict]
    audit_unit_rows: list[dict]
    branch_rating_rows: list[dict]
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

    def write_load_csv(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "timestamp",
            "source_bus_id",
            "source_region",
            "regional_load_mw",
            "regional_scale",
            "base_p_mw",
            "base_q_mvar",
            "load_p_mw",
            "load_q_mvar",
            "p_provenance",
            "q_provenance",
            "q_policy",
            "target_load_name",
            "target_load_mrid",
        ]
        with target.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in self.load_rows:
                writer.writerow({name: row[name] for name in fields})

    def write_operating_snapshot(self, path: str | Path) -> None:
        q_limit_policy = {
            "validate_only": "VALIDATE_ONLY_STATIC_CAPABILITY",
            "apply_source": "APPLY_SOURCE_STATIC_CAPABILITY",
        }[self.audit["generator_ac_controls"]["q_limit_policy"]]
        payload = {
            "schema": "plexos-output2odms-operating-snapshot-v3",
            "time": self.audit["selection"]["time"],
            "generator_setpoints": self.rows,
            "load_setpoints": self.load_rows,
            "unit_statuses": self.status_rows,
            "audit_units": self.audit_unit_rows,
            "voltage_targets": [
                {
                    "target_machine_name": row["target_machine_name"],
                    "target_machine_mrid": row["target_machine_mrid"],
                    "voltage_setpoint_pu": row["source_voltage_setpoint_pu"],
                    "policy": row["ac_control_policy"],
                    "provenance": "RTS_SOURCE_DATA_GEN_CSV",
                }
                for row in self.rows
                if row.get("source_voltage_setpoint_pu") is not None
            ],
            "reactive_capabilities": [
                {
                    "target_machine_name": row["target_machine_name"],
                    "target_machine_mrid": row["target_machine_mrid"],
                    "q_min_mvar": row["source_q_min_mvar"],
                    "q_max_mvar": row["source_q_max_mvar"],
                    "base_q_reference_mvar": row["source_base_q_mvar"],
                    "policy": q_limit_policy,
                    "provenance": "RTS_SOURCE_DATA_GEN_CSV",
                }
                for row in self.rows
                if row.get("source_q_min_mvar") is not None
                and row.get("source_q_max_mvar") is not None
            ],
            "branch_ratings": self.branch_rating_rows,
            "preflight": self.audit["preflight"],
            "provenance": self.audit["sources"],
        }
        Path(path).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def write_status_csv(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fields = [
            "timestamp",
            "source_generator",
            "source_units_generating",
            "resource_class",
            "action",
            "requested_in_service",
            "policy",
            "provenance",
            "target_machine_name",
            "target_machine_mrid",
        ]
        with target.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in self.status_rows:
                writer.writerow({name: row[name] for name in fields})


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
    regional_load_path: str | Path | None = None,
    load_crosswalk_path: str | Path | None = None,
    commitment_path: str | Path | None = None,
    branch_crosswalk_path: str | Path | None = None,
) -> SnapshotResult:
    config = config or SnapshotConfig()
    if config.q_limit_policy not in {"validate_only", "apply_source"}:
        raise ValueError(f"Unsupported Q-limit policy: {config.q_limit_policy}")
    if timestamp.tzinfo is not None:
        raise ValueError("Snapshot timestamp must be a timezone-naive source wall clock")
    time_context = SourceTimeContext(
        timestamp,
        source_time_basis=config.source_time_basis,
        source_timezone=config.source_timezone,
        analysis_timezone=config.analysis_timezone,
    )
    solution = Path(solution_path)
    crosswalk_file = Path(crosswalk_path)
    target_cim = Path(target_cim_path)
    if (regional_load_path is None) != (load_crosswalk_path is None):
        raise ValueError("regional_load_path and load_crosswalk_path must be supplied together")
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
                "resource_type": "GENERATOR",
                "timestamp": record.timestamp.isoformat(),
                "source_wall_clock": record.timestamp.isoformat(),
                "source_time_basis": config.source_time_basis,
                "source_timezone": config.source_timezone,
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
                "source_resource_type": mapping.source_resource_type,
                "source_operating_class": mapping.source_operating_class,
                "status_policy": mapping.status_policy,
                "target_kind": mapping.target_kind,
                "source_base_p_mw": mapping.source_base_p_mw,
                "source_base_q_mvar": mapping.source_base_q_mvar,
                "source_voltage_setpoint_pu": mapping.source_voltage_setpoint_pu,
                "source_q_min_mvar": mapping.source_q_min_mvar,
                "source_q_max_mvar": mapping.source_q_max_mvar,
                "ac_control_policy": mapping.ac_control_policy,
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
    load_rows: list[dict] = []
    regional_values: dict[str, float] = {}
    if regional_load_path is not None and load_crosswalk_path is not None:
        load_crosswalk_file = Path(load_crosswalk_path)
        regional_file = Path(regional_load_path)
        load_mappings = load_load_crosswalk(load_crosswalk_file)
        try:
            regional_values = read_rts_regional_load(regional_file, timestamp)
            load_rows = allocate_rts_nodal_load(regional_values, load_mappings)
        except ValueError as exc:
            report.error("LOAD_SNAPSHOT_INVALID", str(exc))
        for row in load_rows:
            row["timestamp"] = timestamp.isoformat()
            row["source_wall_clock"] = timestamp.isoformat()
            row["source_time_basis"] = config.source_time_basis
            row["source_timezone"] = config.source_timezone
        report.info(
            "LOAD_Q_POLICY",
            "Load P is allocated from authoritative RTS day-ahead regional demand; Q uses preserve_base_pf and is derived AC embedding, not PLEXOS output.",
        )
    else:
        report.warning(
            "LOAD_SNAPSHOT_ABSENT",
            "No load layer was supplied; the operating snapshot is generator-only and is not eligible for balanced AC acceptance.",
        )

    generator_total = sum(row["scheduled_mw"] for row in rows)
    load_p_total = sum(row["load_p_mw"] for row in load_rows)
    load_q_total = sum(row["load_q_mvar"] for row in load_rows)
    preflight_imbalance = generator_total - load_p_total if load_rows else None
    if load_rows and abs(preflight_imbalance) > config.preflight_balance_tolerance_mw:
        report.error(
            "PREFLIGHT_ACTIVE_POWER_IMBALANCE",
            f"Generator-load imbalance {preflight_imbalance} MW exceeds "
            f"{config.preflight_balance_tolerance_mw} MW",
        )

    status_rows: list[dict] = []
    if config.status_mode in {"preserve_odms", "dispatch_on_only"}:
        status_rows = [
            {
                "resource_type": "UNIT_STATUS",
                "timestamp": row["timestamp"],
                "source_generator": row["source_generator"],
                "source_units_generating": None,
                "resource_class": row["source_operating_class"],
                "action": (
                    "set"
                    if config.status_mode == "dispatch_on_only" and row["generation_mw"] > config.bounds_tolerance_mw
                    else "preserve"
                ),
                "requested_in_service": (
                    True
                    if config.status_mode == "dispatch_on_only" and row["generation_mw"] > config.bounds_tolerance_mw
                    else None
                ),
                "policy": (
                    "explicit_positive_dispatch_on_only"
                    if config.status_mode == "dispatch_on_only"
                    else "explicit_run_policy_preserve_odms_status"
                ),
                "provenance": "ADAPTER_RUN_POLICY",
                "target_machine_name": row["target_machine_name"],
                "target_machine_mrid": row["target_machine_mrid"],
            }
            for row in rows
        ]
        report.info(
            "STATUS_RUN_MODE",
            (
                "Positive-dispatch units are turned on; zero-dispatch unit status is preserved."
                if config.status_mode == "dispatch_on_only"
                else "Optimized P is updated and all ODMS unit statuses are preserved."
            ),
        )
    effective_commitment_path = commitment_path
    if effective_commitment_path is None and solution.suffix.casefold() == ".zip":
        effective_commitment_path = solution
    if config.status_mode == "crosswalk_commitment" and effective_commitment_path is not None:
        commitment_file = Path(effective_commitment_path)
        try:
            commitment = read_commitment(
                commitment_file,
                timestamp,
                phase=config.phase,
                period=config.period,
                sample=config.sample,
            )
            status_rows = build_class_aware_statuses(commitment, rows)
        except ValueError as exc:
            report.error("COMMITMENT_INVALID", str(exc))
        report.info(
            "CLASS_AWARE_COMMITMENT",
            "Thermal and hydro use explicit binary commitment; variable resources use ON-only; synchronous condensers are preserved.",
        )
    elif config.status_mode == "crosswalk_commitment":
        report.warning(
            "COMMITMENT_ABSENT",
            "No Units Generating layer was supplied; ODMS base-case unit statuses are preserved.",
        )
    elif config.status_mode not in {"preserve_odms", "dispatch_on_only"}:
        report.error("STATUS_MODE_INVALID", f"Unsupported status mode: {config.status_mode}")

    setpoint_mrids = {row["target_machine_mrid"] for row in rows}
    branch_rating_rows: list[dict] = []
    if branch_crosswalk_path is not None:
        branch_crosswalk_file = Path(branch_crosswalk_path)
        branch_rating_rows = [
            {
                "source_uid": row.source_uid,
                "source_from_bus": row.source_from_bus,
                "source_to_bus": row.source_to_bus,
                "condition_a_mva": row.source_cont_rating_mva,
                "condition_b_mva": row.source_lte_rating_mva,
                "condition_c_mva": row.source_ste_rating_mva,
                "target_name": row.target_name,
                "target_mrid": row.target_mrid,
                "target_kind": row.target_kind,
                "rating_contract": row.rating_contract,
                "provenance": "RTS_SOURCE_DATA_BRANCH_CSV",
            }
            for row in load_branch_rating_crosswalk(branch_crosswalk_file)
        ]
    else:
        report.warning(
            "BRANCH_RATING_CONTRACT_ABSENT",
            "No approved branch rating crosswalk was supplied; overload coverage is unavailable.",
        )
    mapped_by_mrid = {item.odms_synchronous_machine_mrid: item for item in mappings}
    audit_unit_rows: list[dict] = []
    for machine in list_odms_synchronous_machines(target_cim):
        mrid = machine["target_machine_mrid"]
        if mrid in setpoint_mrids:
            continue
        mapping = mapped_by_mrid.get(mrid)
        audit_unit_rows.append(
            {
                **machine,
                "source_generator": mapping.source_name if mapping else None,
                "reason": (
                    "missing_dispatch_preserve" if mapping else "not_in_generator_crosswalk"
                ),
            }
        )

    sources = {
        "solution": {"path": str(solution.resolve()), "sha256": _sha256(solution)},
        "generator_crosswalk": {
            "path": str(crosswalk_file.resolve()),
            "sha256": _sha256(crosswalk_file),
        },
        "target_cim": {"path": str(target_cim.resolve()), "sha256": _sha256(target_cim)},
    }
    if regional_load_path is not None and load_crosswalk_path is not None:
        regional_file = Path(regional_load_path)
        load_crosswalk_file = Path(load_crosswalk_path)
        sources.update(
            {
                "regional_load": {
                    "path": str(regional_file.resolve()),
                    "sha256": _sha256(regional_file),
                },
                "load_crosswalk": {
                    "path": str(load_crosswalk_file.resolve()),
                    "sha256": _sha256(load_crosswalk_file),
                },
            }
        )
    if effective_commitment_path is not None:
        commitment_file = Path(effective_commitment_path)
        sources["commitment"] = {
            "path": str(commitment_file.resolve()),
            "sha256": _sha256(commitment_file),
            "property": (
                "Generator.Units Generating"
                if commitment_file.suffix.casefold() == ".zip"
                else "wide commitment table"
            ),
        }
    if branch_crosswalk_path is not None:
        branch_crosswalk_file = Path(branch_crosswalk_path)
        sources["branch_rating_crosswalk"] = {
            "path": str(branch_crosswalk_file.resolve()),
            "sha256": _sha256(branch_crosswalk_file),
        }
    audit = {
        "schema": "plexos-output2odms-audit-v2",
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "selection": {
            "phase": config.phase,
            "period": config.period,
            "sample": config.sample,
            "time": time_context.to_dict(),
        },
        "sources": sources,
        "mapping": {
            "dispatch_rows": len(dispatch),
            "mapped_rows": len(rows),
            "approved_crosswalk_rows": sum(item.approved for item in mappings),
            "preserved_missing_dispatch": missing_dispatch,
            "scheduled_mw_total": sum(row["scheduled_mw"] for row in rows),
            "ssh_dependency": dependency,
        },
        "load_mapping": {
            "mapped_rows": len(load_rows),
            "regional_mw": regional_values,
            "load_p_mw_total": load_p_total,
            "load_q_mvar_total": load_q_total,
            "q_policy": "preserve_base_pf" if load_rows else None,
        },
        "preflight": {
            "generator_requested_mw": generator_total,
            "load_requested_mw": load_p_total if load_rows else None,
            "load_requested_mvar": load_q_total if load_rows else None,
            "active_power_imbalance_mw": preflight_imbalance,
            "balance_tolerance_mw": config.preflight_balance_tolerance_mw,
            "balanced": bool(load_rows) and abs(preflight_imbalance) <= config.preflight_balance_tolerance_mw,
            "mismatch_distribution_requirement": "SwingBus",
            "eligible_for_ac_acceptance": (
                bool(load_rows)
                and bool(status_rows)
                and abs(preflight_imbalance) <= config.preflight_balance_tolerance_mw
            ),
        },
        "commitment": {
            "status_rows": len(status_rows),
            "set_rows": sum(row["action"] == "set" for row in status_rows),
            "preserved_rows": sum(row["action"] == "preserve" for row in status_rows),
            "policy": config.status_mode if status_rows else None,
        },
        "generator_ac_controls": {
            "q_limit_policy": config.q_limit_policy,
            "q_limit_authority": "RTS_SOURCE_DATA_GEN_CSV",
            "runtime_mutation_authorized": config.q_limit_policy == "apply_source",
        },
        "audit_units": {
            "count": len(audit_unit_rows),
            "targets": audit_unit_rows,
        },
        "branch_ratings": {
            "count": len(branch_rating_rows),
            "contract": "Cont->A; LTE->B; STE->C" if branch_rating_rows else None,
        },
        "validation": report.to_dict(),
    }
    audit["ssh_dependency"] = dependency
    return SnapshotResult(
        sorted(rows, key=lambda item: item["source_generator"]),
        sorted(load_rows, key=lambda item: int(item["source_bus_id"])),
        status_rows,
        audit_unit_rows,
        branch_rating_rows,
        report,
        audit,
    )


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
    normalized_load = output / "load.normalized.csv"
    operating_snapshot = output / "operating_snapshot.json"
    normalized_status = output / "status.normalized.csv"
    ssh = output / "PLEXOS_DISPATCH_SSH.xml"
    result.report.write_json(validation)
    result.write_audit(audit)
    if not result.report.ok:
        return {"validation": str(validation), "audit": str(audit)}
    result.write_normalized_csv(normalized)
    result.write_load_csv(normalized_load)
    result.write_operating_snapshot(operating_snapshot)
    result.write_status_csv(normalized_status)
    ssh_hash = write_ssh(
        result.rows,
        ssh,
        scenario_time=scenario_time,
        dependent_on=result.audit["ssh_dependency"],
    )
    result.audit["outputs"] = {
        "normalized_csv": str(normalized.resolve()),
        "normalized_load_csv": str(normalized_load.resolve()),
        "operating_snapshot": str(operating_snapshot.resolve()),
        "normalized_status_csv": str(normalized_status.resolve()),
        "ssh": str(ssh.resolve()),
        "ssh_sha256": ssh_hash,
    }
    result.write_audit(audit)
    return {
        "normalized": str(normalized),
        "normalized_load": str(normalized_load),
        "operating_snapshot": str(operating_snapshot),
        "normalized_status": str(normalized_status),
        "ssh": str(ssh),
        "validation": str(validation),
        "audit": str(audit),
    }
