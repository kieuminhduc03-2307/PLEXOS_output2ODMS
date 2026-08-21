from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ODMSRunResult:
    converged: bool
    rows: list[dict]
    load_rows: list[dict]
    status_rows: list[dict]
    audit_unit_rows: list[dict]
    preflight: dict
    postflight: dict
    power_flow_summary: dict | None
    sv_stored: bool


def _load_modules():
    try:
        import odmsPy  # type: ignore
        import pssoPy  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "odmsPy/pssoPy are unavailable. Run this command inside the licensed PSS ODMS Python environment."
        ) from exc
    return odmsPy, pssoPy


def _power_flow_summary(case) -> dict | None:
    try:
        summary = case.GetPowerFlowSummary()
        return {
            name: float(getattr(summary, name))
            for name in (
                "GenerationMW", "GenerationMvar", "LoadMW", "LoadMvar", "LossMW",
                "LossMvar", "BusShuntMW", "BusShuntMvar", "LineShuntMW",
                "LineShuntMvar", "LargestMismatchMVA", "TotalMismatchMVA",
                "HighestVoltage", "LowestVoltage",
            )
        }
    except Exception:
        return None


def apply_dispatch_and_solve(
    rows: list[dict],
    *,
    load_rows: list[dict] | None = None,
    status_rows: list[dict] | None = None,
    audit_unit_rows: list[dict] | None = None,
    ssh_path: str | Path | None = None,
    use_ssh: bool = False,
    store_sv: bool = False,
    postflight_balance_tolerance_mw: float = 1e-3,
    module_loader: Callable = _load_modules,
) -> ODMSRunResult:
    """Apply one timestamp to the current ODMS model's in-memory case and solve PF.

    Direct application writes pssoPy.Unit.ScheduledMW. SSH mode loads the generated
    operational-state file through odmsPy.Case.LoadSSHFile. Neither mode edits EQ data.
    """
    odmsPy, pssoPy = module_loader()
    load_rows = load_rows or []
    status_rows = status_rows or []
    audit_unit_rows = audit_unit_rows or []
    odms_case = odmsPy.Case()
    odmsPy.ClearErrors()
    if not odms_case.BuildCase():
        raise RuntimeError("ODMS BuildCase failed: " + odmsPy.GetErrors())
    case = pssoPy.GetCase()
    initialized: list[dict] = []
    initialized_loads: list[dict] = []
    initialized_statuses: list[dict] = []
    audited_units: list[dict] = []
    resolved_audit_units = []
    if use_ssh:
        if ssh_path is None:
            raise ValueError("ssh_path is required when use_ssh=True")
        if not odms_case.LoadSSHFile(str(Path(ssh_path).resolve()), 2020):
            raise RuntimeError("ODMS LoadSSHFile failed: " + odmsPy.GetErrors())
    else:
        resolved_loads = []
        for row in load_rows:
            load = case.GetLoad(row["target_load_name"])
            if load is None or load.IsNull() or load.IsError():
                raise RuntimeError(f"ODMS Load not found: {row['target_load_name']}")
            actual_rdf = (load.GetRdfID() or "").lstrip("#")
            expected_rdf = row["target_load_mrid"].lstrip("#")
            if actual_rdf != expected_rdf:
                raise RuntimeError(
                    f"ODMS Load identity mismatch for {row['target_load_name']}: "
                    f"runtime {actual_rdf!r}, crosswalk {expected_rdf!r}"
                )
            resolved_loads.append((row, load))
        resolved_units = []
        for row in rows:
            unit = case.GetUnit(row["target_machine_name"])
            if unit is None or unit.IsNull() or unit.IsError():
                raise RuntimeError(f"ODMS Unit not found: {row['target_machine_name']}")
            actual_rdf = (unit.GetRdfID() or "").lstrip("#")
            expected_rdf = row["target_machine_mrid"].lstrip("#")
            if actual_rdf != expected_rdf:
                raise RuntimeError(
                    f"ODMS Unit identity mismatch for {row['target_machine_name']}: "
                    f"runtime {actual_rdf!r}, crosswalk {expected_rdf!r}"
                )
            resolved_units.append((row, unit))
        units_by_mrid = {
            row["target_machine_mrid"].lstrip("#"): unit for row, unit in resolved_units
        }
        resolved_statuses = []
        for row in status_rows:
            expected_rdf = row["target_machine_mrid"].lstrip("#")
            unit = units_by_mrid.get(expected_rdf)
            if unit is None:
                raise RuntimeError(
                    f"Status target is not an approved generator setpoint: {row['target_machine_name']}"
                )
            resolved_statuses.append((row, unit))
        for row in audit_unit_rows:
            unit = case.GetUnit(row["target_machine_name"])
            if unit is None or unit.IsNull() or unit.IsError():
                raise RuntimeError(f"ODMS audit Unit not found: {row['target_machine_name']}")
            actual_rdf = (unit.GetRdfID() or "").lstrip("#")
            expected_rdf = row["target_machine_mrid"].lstrip("#")
            if actual_rdf != expected_rdf:
                raise RuntimeError(f"ODMS audit Unit identity mismatch: {row['target_machine_name']}")
            resolved_audit_units.append((row, unit))
        # Apply only after every load and generator identity has passed validation.
        for row, load in resolved_loads:
            if not load.SetLoad(float(row["load_p_mw"]), float(row["load_q_mvar"])):
                raise RuntimeError(f"SetLoad failed for {row['target_load_name']}")
            initialized_loads.append(
                {
                    **row,
                    "initialized_load_p_mw": float(load.TotalMW),
                    "initialized_load_q_mvar": float(load.TotalMvar),
                }
            )
        for row, unit in resolved_statuses:
            previous = bool(unit.IsInService())
            if row["action"] == "set":
                requested = bool(row["requested_in_service"])
                device_status = (
                    pssoPy.DeviceStatus.InService
                    if requested
                    else pssoPy.DeviceStatus.OutOfService
                )
                if not unit.SetDeviceStatus(device_status):
                    raise RuntimeError(f"SetDeviceStatus failed for {row['target_machine_name']}")
                if not unit.Init():
                    raise RuntimeError(f"Status refresh failed for {row['target_machine_name']}")
            elif row["action"] != "preserve":
                raise ValueError(f"Unsupported status action: {row['action']}")
            readback = bool(unit.IsInService())
            if row["action"] == "set" and readback != bool(row["requested_in_service"]):
                raise RuntimeError(f"Status readback mismatch for {row['target_machine_name']}")
            initialized_statuses.append(
                {
                    **row,
                    "previous_in_service": previous,
                    "initialized_in_service": readback,
                }
            )
        for row, unit in resolved_units:
            scheduled_mvar = float(unit.ScheduledMvar)
            if not unit.SetGeneration(float(row["generation_mw"]), scheduled_mvar):
                raise RuntimeError(f"SetGeneration failed for {row['target_machine_name']}")
            initialized.append(
                {
                    **row,
                    "initialized_scheduled_mw": float(unit.ScheduledMW),
                    "initialized_scheduled_mvar": float(unit.ScheduledMvar),
                }
            )
    initialized_generator_total = sum(item["initialized_scheduled_mw"] for item in initialized)
    initialized_load_p_total = sum(item["initialized_load_p_mw"] for item in initialized_loads)
    initialized_load_q_total = sum(item["initialized_load_q_mvar"] for item in initialized_loads)
    preflight = {
        "generator_requested_mw": sum(float(row["generation_mw"]) for row in rows),
        "generator_readback_mw": initialized_generator_total,
        "load_requested_mw": sum(float(row["load_p_mw"]) for row in load_rows),
        "load_readback_mw": initialized_load_p_total,
        "load_requested_mvar": sum(float(row["load_q_mvar"]) for row in load_rows),
        "load_readback_mvar": initialized_load_q_total,
        "active_power_imbalance_mw": initialized_generator_total - initialized_load_p_total,
        "generator_count": len(rows),
        "load_count": len(load_rows),
        "unit_in_service_count": sum(
            bool(case.GetUnit(row["target_machine_name"]).IsInService()) for row in rows
        ),
        "unit_out_of_service_count": sum(
            not bool(case.GetUnit(row["target_machine_name"]).IsInService()) for row in rows
        ),
        "mismatch_distribution_policy": "ODMS_CASE_CONFIGURED_NOT_OVERRIDDEN",
    }
    converged = bool(case.SolvePowerFlow() and case.IsPowerFlowValid())
    if not converged:
        raise RuntimeError("ODMS Power Flow did not converge: " + pssoPy.GetLastError())
    solved = []
    for row in rows:
        unit = case.GetUnit(row["target_machine_name"])
        solved.append(
            {
                **row,
                "initialized_scheduled_mw": float(unit.ScheduledMW),
                "present_mw": float(unit.PresentMW),
                "present_mvar": float(unit.PresentMvar),
                "present_kv": float(unit.PresentkV),
            }
        )
    summary = _power_flow_summary(case)
    for row, unit in (resolved_audit_units if not use_ssh else []):
        audited_units.append(
            {
                **row,
                "in_service": bool(unit.IsInService()),
                "scheduled_mw": float(unit.ScheduledMW),
                "present_mw": float(unit.PresentMW),
                "present_mvar": float(unit.PresentMvar),
                "present_kv": float(unit.PresentkV),
            }
        )
    postflight = {"system_active_balance_tolerance_mw": postflight_balance_tolerance_mw}
    if summary is not None:
        residual = (
            summary["GenerationMW"]
            - summary["LoadMW"]
            - summary["LossMW"]
            - summary["BusShuntMW"]
            - summary["LineShuntMW"]
        )
        postflight["system_active_balance_residual_mw"] = residual
        postflight["system_active_balance_passed"] = (
            abs(residual) <= postflight_balance_tolerance_mw
        )
    else:
        postflight["system_active_balance_passed"] = False
    sv_stored = False
    if store_sv:
        if not postflight["system_active_balance_passed"]:
            raise RuntimeError("ODMS postflight balance must pass before StoreSolutionState")
        if not odms_case.StoreSolutionState():
            raise RuntimeError("ODMS StoreSolutionState failed: " + odmsPy.GetErrors())
        sv_stored = True
    return ODMSRunResult(
        True,
        solved,
        initialized_loads,
        initialized_statuses,
        audited_units,
        preflight,
        postflight,
        summary,
        sv_stored,
    )
