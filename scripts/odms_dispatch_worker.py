"""Internal ODMS worker launched by ODMS.exe; request path comes from script_params."""

from __future__ import annotations

import json
import os
import traceback

import odmsPy
import pssoPy


def write_json(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)


def read_snapshot(path):
    with open(path, "r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if payload.get("schema") != "plexos-output2odms-operating-snapshot-v1":
        raise ValueError("Unsupported operating snapshot schema")
    return (
        payload.get("generator_setpoints", []),
        payload.get("load_setpoints", []),
        payload.get("unit_statuses", []),
        payload.get("audit_units", []),
        payload.get("timestamp"),
    )


def get_power_flow_summary(case):
    try:
        summary = case.GetPowerFlowSummary()
        names = (
            "GenerationMW", "GenerationMvar", "LoadMW", "LoadMvar", "LossMW",
            "LossMvar", "BusShuntMW", "BusShuntMvar", "LineShuntMW",
            "LineShuntMvar", "LargestMismatchMVA", "TotalMismatchMVA",
            "HighestVoltage", "LowestVoltage",
        )
        return {name: float(getattr(summary, name)) for name in names}
    except Exception:
        return None


def main():
    request_path = odmsPy.GetParams()
    with open(request_path, "r", encoding="utf-8") as stream:
        request = json.load(stream)
    response = {
        "valid": False,
        "stage": "starting",
        "odms_version": odmsPy.GetVersion(),
        "model": odmsPy.Model().GetModelName(),
        "server": odmsPy.Model().GetServer(),
    }
    try:
        rows, load_rows, status_rows, audit_unit_rows, timestamp = read_snapshot(
            request["operating_snapshot"]
        )
        if not load_rows:
            raise ValueError("OperatingSnapshot has no load layer; AC acceptance is prohibited")
        if not status_rows:
            raise ValueError(
                "OperatingSnapshot has no reviewed unit-status layer; AC acceptance is prohibited"
            )
        odmsPy.ClearErrors()
        odms_case = odmsPy.Case()
        if not odms_case.BuildCase():
            raise RuntimeError("ODMS BuildCase failed: " + odmsPy.GetErrors())
        response["stage"] = "case_built"
        case = pssoPy.GetCase()
        options = pssoPy.GetPowerFlowOptions()
        mismatch_before = int(options.MismatchDistribution)
        if request.get("mismatch_distribution", "SwingBus") == "SwingBus" and (
            options.MismatchDistribution != pssoPy.MismatchDistributionMethod.SwingBus
        ):
            if not options.SetOptions(
                options.Algorithm,
                bool(options.DivergencePrevention),
                float(options.ConvergenceTolerance),
                int(options.MaximumIterations),
                bool(options.FlatStart),
                int(options.VarLimitCheckIteration),
                pssoPy.MismatchDistributionMethod.SwingBus,
            ):
                raise RuntimeError("Unable to set mismatch distribution to SwingBus")
            if not options.Init():
                raise RuntimeError("Unable to refresh power flow options")
        mismatch_after = int(options.MismatchDistribution)
        resolved_loads = []
        for row in load_rows:
            load = case.GetLoad(row["target_load_name"])
            if load is None or load.IsNull() or load.IsError():
                raise RuntimeError("ODMS Load not found: " + row["target_load_name"])
            actual_rdf = (load.GetRdfID() or "").lstrip("#")
            expected_rdf = row["target_load_mrid"].lstrip("#")
            if actual_rdf != expected_rdf:
                raise RuntimeError(
                    "ODMS Load identity mismatch for %s: runtime %r, crosswalk %r"
                    % (row["target_load_name"], actual_rdf, expected_rdf)
                )
            resolved_loads.append((row, load))
        resolved_units = []
        for row in rows:
            unit = case.GetUnit(row["target_machine_name"])
            if unit is None or unit.IsNull() or unit.IsError():
                raise RuntimeError("ODMS Unit not found: " + row["target_machine_name"])
            actual_rdf = (unit.GetRdfID() or "").lstrip("#")
            expected_rdf = row["target_machine_mrid"].lstrip("#")
            if actual_rdf != expected_rdf:
                raise RuntimeError(
                    "ODMS Unit identity mismatch for %s: runtime %r, crosswalk %r"
                    % (row["target_machine_name"], actual_rdf, expected_rdf)
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
                    "Status target is not an approved generator setpoint: "
                    + row["target_machine_name"]
                )
            resolved_statuses.append((row, unit))
        resolved_audit_units = []
        for row in audit_unit_rows:
            unit = case.GetUnit(row["target_machine_name"])
            if unit is None or unit.IsNull() or unit.IsError():
                raise RuntimeError("ODMS audit Unit not found: " + row["target_machine_name"])
            actual_rdf = (unit.GetRdfID() or "").lstrip("#")
            expected_rdf = row["target_machine_mrid"].lstrip("#")
            if actual_rdf != expected_rdf:
                raise RuntimeError("ODMS audit Unit identity mismatch: " + row["target_machine_name"])
            resolved_audit_units.append((row, unit))

        initialized_loads = []
        for row, load in resolved_loads:
            if not load.SetLoad(float(row["load_p_mw"]), float(row["load_q_mvar"])):
                raise RuntimeError("SetLoad failed for " + row["target_load_name"])
            initialized_loads.append(
                dict(
                    row,
                    initialized_load_p_mw=float(load.TotalMW),
                    initialized_load_q_mvar=float(load.TotalMvar),
                )
            )
        initialized_statuses = []
        for row, unit in resolved_statuses:
            previous = bool(unit.IsInService())
            if row["action"] == "set":
                requested = bool(row["requested_in_service"])
                status = (
                    pssoPy.DeviceStatus.InService
                    if requested
                    else pssoPy.DeviceStatus.OutOfService
                )
                if not unit.SetDeviceStatus(status):
                    raise RuntimeError("SetDeviceStatus failed for " + row["target_machine_name"])
                if not unit.Init():
                    raise RuntimeError("Status refresh failed for " + row["target_machine_name"])
            elif row["action"] != "preserve":
                raise ValueError("Unsupported status action: " + row["action"])
            readback = bool(unit.IsInService())
            if row["action"] == "set" and readback != bool(row["requested_in_service"]):
                raise RuntimeError("Status readback mismatch for " + row["target_machine_name"])
            initialized_statuses.append(
                dict(
                    row,
                    previous_in_service=previous,
                    initialized_in_service=readback,
                )
            )
        initialized = []
        for row, unit in resolved_units:
            if not unit.SetGeneration(float(row["generation_mw"]), float(unit.ScheduledMvar)):
                raise RuntimeError("SetGeneration failed for " + row["target_machine_name"])
            initialized.append(float(unit.ScheduledMW))
        readback_tolerance = float(request.get("readback_tolerance_mw", 1e-4))
        readback_mismatches = [
            {
                "name": rows[i]["target_machine_name"],
                "requested_mw": rows[i]["generation_mw"],
                "scheduled_mw": initialized[i],
                "difference_mw": initialized[i] - rows[i]["generation_mw"],
            }
            for i in range(len(rows))
            if abs(initialized[i] - rows[i]["generation_mw"]) > readback_tolerance
        ]
        if readback_mismatches:
            raise RuntimeError(
                "ODMS ScheduledMW readback differs for %d units; first=%r"
                % (len(readback_mismatches), readback_mismatches[0])
            )
        max_readback_error = max(
            abs(initialized[i] - rows[i]["generation_mw"]) for i in range(len(rows))
        ) if rows else 0.0
        load_readback_mismatches = [
            {
                "name": item["target_load_name"],
                "requested_mw": item["load_p_mw"],
                "readback_mw": item["initialized_load_p_mw"],
                "requested_mvar": item["load_q_mvar"],
                "readback_mvar": item["initialized_load_q_mvar"],
            }
            for item in initialized_loads
            if abs(item["initialized_load_p_mw"] - item["load_p_mw"]) > readback_tolerance
            or abs(item["initialized_load_q_mvar"] - item["load_q_mvar"]) > readback_tolerance
        ]
        if load_readback_mismatches:
            raise RuntimeError(
                "ODMS load readback differs for %d loads; first=%r"
                % (len(load_readback_mismatches), load_readback_mismatches[0])
            )
        requested_generation = sum(row["generation_mw"] for row in rows)
        initialized_generation = sum(initialized)
        requested_load_p = sum(row["load_p_mw"] for row in load_rows)
        initialized_load_p = sum(item["initialized_load_p_mw"] for item in initialized_loads)
        requested_load_q = sum(row["load_q_mvar"] for row in load_rows)
        initialized_load_q = sum(item["initialized_load_q_mvar"] for item in initialized_loads)
        preflight = {
            "timestamp": timestamp,
            "generator_requested_mw": requested_generation,
            "generator_readback_mw": initialized_generation,
            "load_requested_mw": requested_load_p,
            "load_readback_mw": initialized_load_p,
            "load_requested_mvar": requested_load_q,
            "load_readback_mvar": initialized_load_q,
            "active_power_imbalance_mw": initialized_generation - initialized_load_p,
            "generator_count": len(rows),
            "load_count": len(load_rows),
            "unit_in_service_count": sum(bool(unit.IsInService()) for _, unit in resolved_units),
            "unit_out_of_service_count": sum(not bool(unit.IsInService()) for _, unit in resolved_units),
            "mismatch_distribution_requested": request.get(
                "mismatch_distribution", "SwingBus"
            ),
            "mismatch_distribution_before": mismatch_before,
            "mismatch_distribution_after": mismatch_after,
            "mismatch_distribution_after_name": str(options.MismatchDistribution),
        }
        swing_buses = []
        seen_bus_mrids = set()
        for _, unit in resolved_units + resolved_audit_units:
            bus = unit.GetBus()
            if bus is None or bus.IsNull() or bus.IsError():
                continue
            bus_mrid = (bus.GetRdfID() or "").lstrip("#")
            if bus_mrid in seen_bus_mrids:
                continue
            seen_bus_mrids.add(bus_mrid)
            if bus.BusType == pssoPy.BusType.SwingBus:
                swing_buses.append(
                    {
                        "name": bus.Name,
                        "mrid": bus_mrid,
                    }
                )
        preflight["swing_buses"] = swing_buses
        response.update(
            {
                "stage": "operating_snapshot_initialized",
                "initialized_row_count": len(initialized),
                "initialized_load_count": len(initialized_loads),
                "requested_scheduled_mw_total": requested_generation,
                "initialized_scheduled_mw_total": initialized_generation,
                "requested_load_mw_total": requested_load_p,
                "initialized_load_mw_total": initialized_load_p,
                "requested_load_mvar_total": requested_load_q,
                "initialized_load_mvar_total": initialized_load_q,
                "scheduled_mw_readback_tolerance": readback_tolerance,
                "scheduled_mw_max_readback_error": max_readback_error,
                "preflight": preflight,
                "load_rows": initialized_loads,
                "status_rows": initialized_statuses,
            }
        )
        converged = bool(case.SolvePowerFlow() and case.IsPowerFlowValid())
        if not converged:
            response["power_flow_converged"] = False
            raise RuntimeError("ODMS Power Flow did not converge: " + pssoPy.GetLastError())
        solved_rows = []
        for row in rows:
            unit = case.GetUnit(row["target_machine_name"])
            solved_rows.append(
                dict(
                    row,
                    initialized_scheduled_mw=float(unit.ScheduledMW),
                    swing_bus_priority=int(unit.SwingBusPriority),
                    present_mw=float(unit.PresentMW),
                    present_mvar=float(unit.PresentMvar),
                    present_kv=float(unit.PresentkV),
                )
            )
        power_flow_summary = get_power_flow_summary(case)
        audited_units = []
        for row, unit in resolved_audit_units:
            audited_units.append(
                dict(
                    row,
                    in_service=bool(unit.IsInService()),
                    scheduled_mw=float(unit.ScheduledMW),
                    swing_bus_priority=int(unit.SwingBusPriority),
                    present_mw=float(unit.PresentMW),
                    present_mvar=float(unit.PresentMvar),
                    present_kv=float(unit.PresentkV),
                )
            )
        all_unit_rows = [
            {
                "name": row["target_machine_name"],
                "reason": "dispatch_setpoint",
                "scheduled_mw": row["initialized_scheduled_mw"],
                "present_mw": row["present_mw"],
            }
            for row in solved_rows
        ] + [
            {
                "name": row["target_machine_name"],
                "reason": row["reason"],
                "scheduled_mw": row["scheduled_mw"],
                "present_mw": row["present_mw"],
            }
            for row in audited_units
        ]
        slack_candidates = sorted(
            [
                dict(
                    row,
                    delta_mw=row["present_mw"] - row["scheduled_mw"],
                )
                for row in all_unit_rows
            ],
            key=lambda item: abs(item["delta_mw"]),
            reverse=True,
        )[:10]
        postflight = {
            "all_unit_count": len(all_unit_rows),
            "all_unit_present_mw": sum(row["present_mw"] for row in all_unit_rows),
            "unit_present_sum_note": (
                "Unit.PresentMW does not expose ODMS swing compensation; "
                "PowerFlowSummary.GenerationMW is authoritative for system balance."
            ),
            "slack_candidates": slack_candidates,
        }
        postflight["swing_priority_units"] = [
            {
                "name": row["target_machine_name"],
                "priority": row["swing_bus_priority"],
                "present_mw": row["present_mw"],
            }
            for row in solved_rows + audited_units
            if row["swing_bus_priority"] == 0
        ]
        if power_flow_summary is not None:
            residual = (
                power_flow_summary["GenerationMW"]
                - power_flow_summary["LoadMW"]
                - power_flow_summary["LossMW"]
                - power_flow_summary["BusShuntMW"]
                - power_flow_summary["LineShuntMW"]
            )
            tolerance = float(request.get("postflight_balance_tolerance_mw", 1e-3))
            postflight["system_active_balance_residual_mw"] = residual
            postflight["system_active_balance_tolerance_mw"] = tolerance
            postflight["system_active_balance_passed"] = abs(residual) <= tolerance
            if abs(residual) > tolerance:
                response.update(
                    {
                        "stage": "power_flow_postflight_failed",
                        "power_flow_converged": True,
                        "power_flow_summary": power_flow_summary,
                        "postflight": postflight,
                    }
                )
                raise RuntimeError(
                    "ODMS postflight active balance residual exceeds tolerance: %r MW"
                    % residual
                )
        else:
            raise RuntimeError("ODMS PowerFlowSummary is unavailable after convergence")
        sv_stored = False
        if request.get("store_sv", False):
            if not odms_case.StoreSolutionState():
                raise RuntimeError("ODMS StoreSolutionState failed: " + odmsPy.GetErrors())
            sv_stored = True
        response.update(
            {
                "valid": True,
                "stage": "power_flow_converged",
                "power_flow_converged": converged,
                "sv_stored": sv_stored,
                "power_flow_summary": power_flow_summary,
                "row_count": len(solved_rows),
                "scheduled_mw_readback_tolerance": readback_tolerance,
                "scheduled_mw_max_readback_error": max_readback_error,
                "rows": solved_rows,
                "load_rows": initialized_loads,
                "status_rows": initialized_statuses,
                "preflight": preflight,
                "postflight": postflight,
                "audit_unit_rows": audited_units,
                "odms_output": odmsPy.GetOutput(),
                "odms_errors": odmsPy.GetErrors(),
            }
        )
    except Exception as exc:
        response.update(
            {
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
                "odms_output": odmsPy.GetOutput(),
                "odms_errors": odmsPy.GetErrors(),
            }
        )
    finally:
        try:
            odmsPy.Case().CloseCase()
        except Exception:
            pass
        write_json(request["response_json"], response)


main()
