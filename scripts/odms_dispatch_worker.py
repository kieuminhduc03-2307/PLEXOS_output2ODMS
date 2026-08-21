"""Internal ODMS worker launched by ODMS.exe; request path comes from script_params."""

from __future__ import annotations

import json
import math
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
    if payload.get("schema") not in (
        "plexos-output2odms-operating-snapshot-v1",
        "plexos-output2odms-operating-snapshot-v2",
        "plexos-output2odms-operating-snapshot-v3",
    ):
        raise ValueError("Unsupported operating snapshot schema")
    return (
        payload.get("generator_setpoints", []),
        payload.get("load_setpoints", []),
        payload.get("unit_statuses", []),
        payload.get("audit_units", []),
        payload.get("voltage_targets", []),
        payload.get("reactive_capabilities", []),
        payload.get("branch_ratings", []),
        (payload.get("time") or {}).get("source_wall_clock") or payload.get("timestamp"),
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


def devices(case, element_type):
    result = []
    for item in case.GetDevices(element_type):
        valid, element = item if isinstance(item, tuple) else (True, item)
        if valid and element is not None and not element.IsNull() and not element.IsError():
            result.append(element)
    return result


def engineering_gates(case, resolved_units, request):
    min_voltage = float(request.get("min_voltage_pu", 0.9))
    max_voltage = float(request.get("max_voltage_pu", 1.1))
    max_loading = float(request.get("max_loading_percent", 100.0))
    gen_tolerance = float(request.get("generator_limit_tolerance_mw", 1e-4))
    bus_rows = []
    for bus in devices(case, pssoPy.ElementType.Bus):
        voltage = float(bus.PresentVoltagePU)
        if math.isfinite(voltage) and voltage > 0.0:
            bus_rows.append({"name": bus.Name, "voltage_pu": voltage})
    voltage_violations = [
        row for row in bus_rows
        if row["voltage_pu"] < min_voltage or row["voltage_pu"] > max_voltage
    ]
    generator_violations = []
    for row, unit in resolved_units:
        scheduled = float(unit.ScheduledMW)
        minimum = float(unit.MinimumMW)
        maximum = float(unit.MaximumMW)
        in_service = bool(unit.IsInService())
        reasons = []
        if float(row["generation_mw"]) > gen_tolerance and not in_service:
            reasons.append("positive dispatch on out-of-service unit")
        if in_service and scheduled > maximum + gen_tolerance:
            reasons.append("scheduled MW above operational maximum")
        if in_service and scheduled > gen_tolerance and scheduled < minimum - gen_tolerance:
            reasons.append("scheduled MW below operational minimum")
        if reasons:
            generator_violations.append({
                "name": row["target_machine_name"], "scheduled_mw": scheduled,
                "minimum_mw": minimum, "maximum_mw": maximum,
                "in_service": in_service, "reasons": reasons,
            })
    branch_rows = []
    branch_device_count = 0
    unrated_branches = []
    for element_type in (
        pssoPy.ElementType.Line,
        pssoPy.ElementType.Transformer,
        pssoPy.ElementType.PhaseShifter,
    ):
        for branch in devices(case, element_type):
            if not bool(branch.IsInService()):
                continue
            branch_device_count += 1
            limits = branch.GetFlowLimits(pssoPy.LimitCondition.ConditionA)
            active_limit = float(limits.ActiveLimit)
            loading = float(limits.PercentOfLimit)
            if active_limit > 0.0 and math.isfinite(loading):
                branch_rows.append({
                    "name": branch.Name,
                    "element_type": str(element_type),
                    "active_limit_mva": active_limit,
                    "percent_of_limit": loading,
                })
            else:
                unrated_branches.append({"name": branch.Name, "element_type": str(element_type)})
    overloads = [row for row in branch_rows if row["percent_of_limit"] > max_loading]
    limit_data_complete = branch_device_count > 0 and not unrated_branches
    passed = (
        bool(bus_rows) and limit_data_complete and not voltage_violations
        and not generator_violations and not overloads
    )
    return {
        "passed": passed,
        "voltage_range_pu": [min_voltage, max_voltage],
        "minimum_voltage_pu": min((row["voltage_pu"] for row in bus_rows), default=None),
        "maximum_voltage_pu": max((row["voltage_pu"] for row in bus_rows), default=None),
        "voltage_bus_count": len(bus_rows),
        "voltage_violation_count": len(voltage_violations),
        "voltage_violations": voltage_violations,
        "generator_violation_count": len(generator_violations),
        "generator_violations": generator_violations,
        "maximum_loading_percent_allowed": max_loading,
        "maximum_loading_percent": max(
            (row["percent_of_limit"] for row in branch_rows), default=None
        ),
        "monitored_branch_count": len(branch_rows),
        "in_service_branch_count": branch_device_count,
        "unrated_branch_count": len(unrated_branches),
        "unrated_branches": unrated_branches,
        "limit_data_complete": limit_data_complete,
        "overload_count": len(overloads),
        "overloads": overloads,
    }


def network_control_audit(case):
    transformers = []
    for transformer in devices(case, pssoPy.ElementType.Transformer):
        control = transformer.GetControl()
        if control is None or control.IsNull() or control.IsError():
            control_row = None
        else:
            section = control.GetSection()
            control_row = {
                "control_type": str(control.ControlType),
                "tap_changing": bool(control.TapChanging),
                "fixed": bool(control.Fixed),
                "current_tap_position": float(control.CurrentTapPosition),
                "minimum_tap_position": float(control.MinimumTapPosition),
                "maximum_tap_position": float(control.MaximumTapPosition),
                "minimum_kv": float(control.MinimumkV),
                "maximum_kv": float(control.MaximumkV),
                "present_kv": float(control.PresentkV),
                "controlled_bus": int(section.MappedBusNumber),
            }
        transformers.append({"name": transformer.Name, "control": control_row})
    banks = []
    for bank in devices(case, pssoPy.ElementType.Bank):
        controlled = bank.GetControlledSection()
        banks.append(
            {
                "name": bank.Name,
                "in_service": bool(bank.IsInService()),
                "control_mode": str(bank.ControlMode),
                "is_fixed": bool(bank.IsFixed),
                "maximum_sections": int(bank.MaximumSections),
                "switched_on_sections": int(bank.SwitchedOnSections),
                "mvar_per_section": float(bank.MvarPerSection),
                "bank_mvar": float(bank.BankMvar),
                "controlled_bus": int(controlled.MappedBusNumber),
                "minimum_kv": float(bank.MinimumkV),
                "maximum_kv": float(bank.MaximumkV),
            }
        )
    svcs = []
    for svc in devices(case, pssoPy.ElementType.SVC):
        controlled = svc.GetControlledSection()
        svcs.append(
            {
                "name": svc.Name,
                "in_service": bool(svc.IsInService()),
                "control_mode": str(svc.ControlMode),
                "set_point": float(svc.SetPoint),
                "slope": float(svc.Slope),
                "present_mvar": float(svc.PresentMvar),
                "controlled_bus": int(controlled.MappedBusNumber),
            }
        )
    return {
        "transformer_count": len(transformers),
        "transformer_control_count": sum(row["control"] is not None for row in transformers),
        "transformers": transformers,
        "bank_count": len(banks),
        "banks": banks,
        "svc_count": len(svcs),
        "svcs": svcs,
    }


def main():
    request_path = odmsPy.GetParams()
    with open(request_path, "r", encoding="utf-8-sig") as stream:
        request = json.load(stream)
    response = {
        "valid": False,
        "adapter_valid": False,
        "ac_valid": False,
        "outcome_class": "MAPPING_INVALID",
        "outcome_flags": ["MAPPING_INVALID"],
        "stage": "starting",
        "odms_version": odmsPy.GetVersion(),
        "model": odmsPy.Model().GetModelName(),
        "server": odmsPy.Model().GetServer(),
    }
    try:
        (
            rows, load_rows, status_rows, audit_unit_rows, voltage_targets,
            reactive_capabilities, branch_ratings, timestamp,
        ) = read_snapshot(
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
        unit_base_kv_by_mrid = {
            row["target_machine_mrid"].lstrip("#"): float(unit.GetBus().BasekV)
            for row, unit in resolved_units
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
        voltage_by_mrid = {
            row["target_machine_mrid"].lstrip("#"): row for row in voltage_targets
        }
        reactive_by_mrid = {
            row["target_machine_mrid"].lstrip("#"): row for row in reactive_capabilities
        }
        if set(voltage_by_mrid) - set(units_by_mrid):
            raise RuntimeError("Voltage targets contain generators outside approved setpoints")
        if set(reactive_by_mrid) - set(units_by_mrid):
            raise RuntimeError("Reactive capabilities contain generators outside approved setpoints")
        if request.get("require_ac_control_contract", True) and (
            set(voltage_by_mrid) != set(units_by_mrid)
            or set(reactive_by_mrid) != set(units_by_mrid)
        ):
            response.update(
                {
                    "stage": "input_control_data_missing",
                    "adapter_valid": True,
                    "ac_valid": False,
                    "outcome_class": "INPUT_CONTROL_DATA_MISSING",
                    "outcome_flags": ["INPUT_CONTROL_DATA_MISSING"],
                    "control_contract": {
                        "generator_count": len(units_by_mrid),
                        "voltage_target_count": len(voltage_by_mrid),
                        "reactive_capability_count": len(reactive_by_mrid),
                    },
                }
            )
            raise ValueError("OperatingSnapshot has incomplete generator AC control contract")
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

        runtime_branches = {}
        for element_type in (
            pssoPy.ElementType.Line,
            pssoPy.ElementType.Transformer,
            pssoPy.ElementType.PhaseShifter,
        ):
            for branch in devices(case, element_type):
                runtime_branches[(branch.GetRdfID() or "").lstrip("#")] = branch
        initialized_branch_ratings = []
        for row in branch_ratings:
            expected_mrid = row["target_mrid"].lstrip("#")
            branch = runtime_branches.get(expected_mrid)
            if branch is None:
                raise RuntimeError("ODMS branch rating target not found: " + row["target_name"])
            if branch.Name != row["target_name"]:
                raise RuntimeError("ODMS branch rating identity mismatch: " + row["target_name"])
            limits = branch.GetFlowLimits(pssoPy.LimitCondition.ConditionA)
            before = {
                label: float(getattr(limits, label))
                for label in (
                    "FromLimitA", "ToLimitA", "FromLimitB", "ToLimitB",
                    "FromLimitC", "ToLimitC",
                )
            }
            for end in ("From", "To"):
                setattr(limits, end + "LimitA", float(row["condition_a_mva"]))
                setattr(limits, end + "LimitB", float(row["condition_b_mva"]))
                setattr(limits, end + "LimitC", float(row["condition_c_mva"]))
            if not limits.Update():
                raise RuntimeError("ODMS flow-limit update failed: " + row["target_name"])
            readback = branch.GetFlowLimits(pssoPy.LimitCondition.ConditionA)
            after = {
                label: float(getattr(readback, label))
                for label in (
                    "FromLimitA", "ToLimitA", "FromLimitB", "ToLimitB",
                    "FromLimitC", "ToLimitC",
                )
            }
            expected = {
                "FromLimitA": float(row["condition_a_mva"]),
                "ToLimitA": float(row["condition_a_mva"]),
                "FromLimitB": float(row["condition_b_mva"]),
                "ToLimitB": float(row["condition_b_mva"]),
                "FromLimitC": float(row["condition_c_mva"]),
                "ToLimitC": float(row["condition_c_mva"]),
            }
            if any(abs(after[key] - value) > 1e-3 for key, value in expected.items()):
                raise RuntimeError("ODMS flow-limit readback mismatch: " + row["target_name"])
            initialized_branch_ratings.append(dict(row, before=before, after=after))

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
        initialized_ac_controls = []
        ac_tolerance = float(request.get("ac_control_readback_tolerance", 1e-2))
        for row, unit in resolved_units:
            mrid = row["target_machine_mrid"].lstrip("#")
            reactive = reactive_by_mrid.get(mrid)
            voltage = voltage_by_mrid.get(mrid)
            before = {
                "minimum_mvar": float(unit.MinimumMvar),
                "maximum_mvar": float(unit.MaximumMvar),
                "scheduled_kv": float(unit.ScheduledKV),
                "regulating_code": str(unit.RegulatingCode),
                "regulating_code_value": int(unit.RegulatingCode),
            }
            if reactive is not None:
                q_min = float(reactive["q_min_mvar"])
                q_max = float(reactive["q_max_mvar"])
                if q_min > q_max:
                    raise ValueError("Invalid reactive capability for " + row["target_machine_name"])
                if not unit.SetReactiveLimits(q_min, q_max):
                    raise RuntimeError("SetReactiveLimits failed for " + row["target_machine_name"])
            voltage_action = "not_provided"
            requested_kv = None
            if voltage is not None:
                if (
                    unit.RegulatingCode == pssoPy.UnitRegulatingCode.Regulating
                    and bool(unit.IsInService())
                ):
                    requested_kv = (
                        float(voltage["voltage_setpoint_pu"]) * unit_base_kv_by_mrid[mrid]
                    )
                    if abs(float(unit.ScheduledKV) - requested_kv) <= ac_tolerance:
                        voltage_action = "validated_existing_regulating_setpoint"
                    else:
                        if not unit.SetScheduledVoltage(requested_kv):
                            raise RuntimeError(
                                "SetScheduledVoltage failed for %s: current=%r requested=%r code=%r status=%r"
                                % (
                                    row["target_machine_name"], float(unit.ScheduledKV),
                                    requested_kv, str(unit.RegulatingCode), bool(unit.IsInService()),
                                )
                            )
                        voltage_action = "applied_regulating_unit"
                elif unit.RegulatingCode == pssoPy.UnitRegulatingCode.Regulating:
                    voltage_action = "preserved_out_of_service_regulating_unit"
                else:
                    voltage_action = "preserved_nonregulating_unit"
            if not unit.Init():
                raise RuntimeError("AC control refresh failed for " + row["target_machine_name"])
            after = {
                "minimum_mvar": float(unit.MinimumMvar),
                "maximum_mvar": float(unit.MaximumMvar),
                "scheduled_kv": float(unit.ScheduledKV),
                "regulating_code": str(unit.RegulatingCode),
                "regulating_code_value": int(unit.RegulatingCode),
            }
            if reactive is not None and (
                abs(after["minimum_mvar"] - float(reactive["q_min_mvar"])) > ac_tolerance
                or abs(after["maximum_mvar"] - float(reactive["q_max_mvar"])) > ac_tolerance
            ):
                raise RuntimeError("Reactive limit readback mismatch for " + row["target_machine_name"])
            if requested_kv is not None and abs(after["scheduled_kv"] - requested_kv) > ac_tolerance:
                raise RuntimeError("ScheduledKV readback mismatch for " + row["target_machine_name"])
            initialized_ac_controls.append(
                {
                    "name": row["target_machine_name"],
                    "mrid": mrid,
                    "before": before,
                    "after": after,
                    "voltage_action": voltage_action,
                    "requested_voltage_pu": (
                        float(voltage["voltage_setpoint_pu"]) if voltage is not None else None
                    ),
                    "requested_kv": requested_kv,
                    "q_schedule_policy": "ODMS_BASE_SCHEDULE_PRESERVED_NOT_PLEXOS_Q_TIMESERIES",
                }
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
                "adapter_valid": True,
                "outcome_class": "ADAPTER_VALID_PENDING_AC",
                "outcome_flags": [],
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
                "ac_control_rows": initialized_ac_controls,
                "branch_rating_rows": initialized_branch_ratings,
            }
        )
        converged = bool(case.SolvePowerFlow() and case.IsPowerFlowValid())
        if not converged:
            response.update(
                {
                    "power_flow_converged": False,
                    "adapter_valid": True,
                    "ac_valid": False,
                    "outcome_class": "ADAPTER_VALID_AC_NONCONVERGED",
                    "outcome_flags": ["ADAPTER_VALID_AC_NONCONVERGED"],
                    "network_summary": {
                        "active_islands": int(case.GetNetworkSummary().ActiveIslands),
                    },
                    "power_flow_summary": get_power_flow_summary(case),
                }
            )
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
            "active_islands": int(case.GetNetworkSummary().ActiveIslands),
            "network_controls": network_control_audit(case),
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
            postflight["unattributed_swing_mw"] = (
                power_flow_summary["GenerationMW"] - postflight["all_unit_present_mw"]
            )
            postflight["unattributed_swing_note"] = (
                "System-level difference only; it is not attributed to any machine."
            )
            residual = (
                power_flow_summary["GenerationMW"]
                - power_flow_summary["LoadMW"]
                - power_flow_summary["LossMW"]
                - power_flow_summary["BusShuntMW"]
                - power_flow_summary["LineShuntMW"]
            )
            absolute_tolerance = float(request.get("postflight_balance_tolerance_mw", 1e-3))
            relative_tolerance = float(
                request.get("postflight_balance_relative_tolerance", 1e-4)
            )
            scale_mw = max(
                abs(power_flow_summary["GenerationMW"]),
                abs(power_flow_summary["LoadMW"]),
                1.0,
            )
            tolerance = max(absolute_tolerance, relative_tolerance * scale_mw)
            postflight["system_active_balance_residual_mw"] = residual
            postflight["system_active_balance_tolerance_mw"] = tolerance
            postflight["system_active_balance_absolute_tolerance_mw"] = absolute_tolerance
            postflight["system_active_balance_relative_tolerance"] = relative_tolerance
            postflight["system_active_balance_scale_mw"] = scale_mw
            postflight["system_active_balance_passed"] = abs(residual) <= tolerance
            if abs(residual) > tolerance:
                response.update(
                    {
                        "stage": "power_flow_postflight_failed",
                        "adapter_valid": True,
                        "ac_valid": False,
                        "outcome_class": "ADAPTER_VALID_ACCOUNTING_RESIDUAL",
                        "outcome_flags": ["ADAPTER_VALID_ACCOUNTING_RESIDUAL"],
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
        gates = engineering_gates(case, resolved_units, request)
        postflight["engineering_gates"] = gates
        if not gates["passed"]:
            outcome_flags = []
            if not gates["limit_data_complete"]:
                outcome_flags.append("LIMIT_DATA_MISSING")
            if gates["generator_violation_count"]:
                outcome_flags.append("ADAPTER_VALID_AC_GENERATOR_LIMIT_VIOLATION")
            if gates["overload_count"]:
                outcome_flags.append("ADAPTER_VALID_AC_OVERLOAD")
            if gates["voltage_violation_count"]:
                outcome_flags.append("ADAPTER_VALID_AC_VOLTAGE_VIOLATION")
            if not gates["limit_data_complete"]:
                outcome_class = "LIMIT_DATA_MISSING"
            elif gates["generator_violation_count"]:
                outcome_class = "ADAPTER_VALID_AC_GENERATOR_LIMIT_VIOLATION"
            elif gates["overload_count"]:
                outcome_class = "ADAPTER_VALID_AC_OVERLOAD"
            elif gates["voltage_violation_count"]:
                outcome_class = "ADAPTER_VALID_AC_VOLTAGE_VIOLATION"
            else:
                outcome_class = "ADAPTER_VALID_AC_ENGINEERING_VIOLATION"
            response.update(
                {
                    "stage": "engineering_gates_failed",
                    "adapter_valid": True,
                    "ac_valid": False,
                    "outcome_class": outcome_class,
                    "outcome_flags": outcome_flags,
                    "power_flow_converged": True,
                    "power_flow_summary": power_flow_summary,
                    "postflight": postflight,
                }
            )
            raise RuntimeError(
                "ODMS engineering gates failed: voltage=%d generator=%d overload=%d"
                % (
                    gates["voltage_violation_count"],
                    gates["generator_violation_count"],
                    gates["overload_count"],
                )
            )
        sv_stored = False
        if request.get("store_sv", False):
            if not odms_case.StoreSolutionState():
                raise RuntimeError("ODMS StoreSolutionState failed: " + odmsPy.GetErrors())
            sv_stored = True
        response.update(
            {
                "valid": True,
                "adapter_valid": True,
                "ac_valid": True,
                "outcome_class": "ADAPTER_VALID_AC_VALID",
                "outcome_flags": [],
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
