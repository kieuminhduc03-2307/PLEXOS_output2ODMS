"""Internal ODMS worker launched by ODMS.exe; request path comes from script_params."""

from __future__ import annotations

import csv
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


def read_rows(path):
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            rows.append(
                {
                    "timestamp": row["timestamp"],
                    "source_generator": row["source_generator"],
                    "generation_mw": float(row["generation_mw"]),
                    "target_machine_name": row["target_machine_name"],
                    "target_machine_mrid": row["target_machine_mrid"],
                }
            )
    return rows


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
        rows = read_rows(request["normalized_csv"])
        odmsPy.ClearErrors()
        odms_case = odmsPy.Case()
        if not odms_case.BuildCase():
            raise RuntimeError("ODMS BuildCase failed: " + odmsPy.GetErrors())
        response["stage"] = "case_built"
        case = pssoPy.GetCase()
        initialized = []
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
        )
        response.update(
            {
                "stage": "dispatch_initialized",
                "initialized_row_count": len(initialized),
                "requested_scheduled_mw_total": sum(row["generation_mw"] for row in rows),
                "initialized_scheduled_mw_total": sum(initialized),
                "scheduled_mw_readback_tolerance": readback_tolerance,
                "scheduled_mw_max_readback_error": max_readback_error,
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
                    present_mw=float(unit.PresentMW),
                    present_mvar=float(unit.PresentMvar),
                    present_kv=float(unit.PresentkV),
                )
            )
        sv_stored = False
        if request.get("store_sv", False):
            if not odms_case.StoreSolutionState():
                raise RuntimeError("ODMS StoreSolutionState failed: " + odmsPy.GetErrors())
            sv_stored = True
        try:
            power_flow_summary = case.GetPowerFlowSummaryDict()[0]
        except Exception:
            power_flow_summary = None
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
