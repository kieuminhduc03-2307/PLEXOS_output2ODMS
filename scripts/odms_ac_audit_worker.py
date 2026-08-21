"""Read-only same-case AC/control audit launched by ODMS.exe."""

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


def devices(case, element_type):
    result = []
    for item in case.GetDevices(element_type):
        valid, element = item if isinstance(item, tuple) else (True, item)
        if valid and element is not None and not element.IsNull() and not element.IsError():
            result.append(element)
    return result


def section_data(section):
    if section is None or section.IsNull() or section.IsError():
        return None
    return {
        "name": section.Name,
        "mapped_bus_number": int(
            section.MappedBusNumber
            if hasattr(section, "MappedBusNumber")
            else section.BusNumber
        ),
        "base_kv": float(section.BasekV),
        "present_voltage_pu": float(section.PresentVoltagePU),
        "present_kv": float(section.PresentkV),
    }


def control_data(control):
    if control is None or control.IsNull() or control.IsError():
        return None
    controlled = control.GetSection() if hasattr(control, "GetSection") else None
    return {
        "control_type": str(control.ControlType),
        "tap_changing": bool(control.TapChanging),
        "fixed": bool(control.Fixed),
        "current_tap_position": float(control.CurrentTapPosition),
        "minimum_tap_position": float(control.MinimumTapPosition),
        "maximum_tap_position": float(control.MaximumTapPosition),
        "controlled_section": section_data(controlled),
        "minimum_kv": float(control.MinimumkV) if hasattr(control, "MinimumkV") else None,
        "maximum_kv": float(control.MaximumkV) if hasattr(control, "MaximumkV") else None,
        "present_kv": float(control.PresentkV) if hasattr(control, "PresentkV") else None,
    }


def main():
    request_path = odmsPy.GetParams()
    with open(request_path, "r", encoding="utf-8-sig") as stream:
        request = json.load(stream)
    response = {"valid": False, "stage": "starting"}
    try:
        odms_case = odmsPy.Case()
        if not odms_case.BuildCase():
            raise RuntimeError("ODMS BuildCase failed: " + odmsPy.GetErrors())
        case = pssoPy.GetCase()
        solved = bool(case.SolvePowerFlow() and case.IsPowerFlowValid())
        buses = []
        for bus in devices(case, pssoPy.ElementType.Bus):
            buses.append(
                {
                    "name": bus.Name,
                    "bus_number": int(bus.BusNumber),
                    "base_kv": float(bus.BasekV),
                    "bus_type": str(bus.BusType),
                    "present_voltage_pu": float(bus.PresentVoltagePU),
                    "present_kv": float(bus.PresentkV),
                    "voltage_angle": float(bus.VoltageAngle),
                    "generation_mw": float(bus.GenerationMW),
                    "generation_mvar": float(bus.GenerationMvar),
                    "load_mw": float(bus.LoadMW),
                    "load_mvar": float(bus.LoadMvar),
                    "bus_shunt_mvar": float(bus.BusShuntMvar),
                    "line_shunt_mvar": float(bus.LineShuntMvar),
                }
            )
        loads = []
        for load in devices(case, pssoPy.ElementType.Load):
            loads.append(
                {
                    "name": load.Name,
                    "mrid": (load.GetRdfID() or "").lstrip("#"),
                    "in_service": bool(load.IsInService()),
                    "bus": section_data(load.GetBus()),
                    "total_mw": float(load.TotalMW),
                    "total_mvar": float(load.TotalMvar),
                }
            )
        units = []
        for unit in devices(case, pssoPy.ElementType.Unit):
            bus = unit.GetBus()
            units.append(
                {
                    "name": unit.Name,
                    "mrid": (unit.GetRdfID() or "").lstrip("#"),
                    "in_service": bool(unit.IsInService()),
                    "bus": section_data(bus),
                    "scheduled_mw": float(unit.ScheduledMW),
                    "scheduled_mvar": float(unit.ScheduledMvar),
                    "scheduled_kv": float(unit.ScheduledKV),
                    "minimum_mvar": float(unit.MinimumMvar),
                    "maximum_mvar": float(unit.MaximumMvar),
                    "regulating_code": str(unit.RegulatingCode),
                    "regulating_code_value": int(unit.RegulatingCode),
                    "present_mw": float(unit.PresentMW),
                    "present_mvar": float(unit.PresentMvar),
                    "present_kv": float(unit.PresentkV),
                }
            )
        branches = []
        for kind, element_type in (
            ("Line", pssoPy.ElementType.Line),
            ("Transformer", pssoPy.ElementType.Transformer),
            ("PhaseShifter", pssoPy.ElementType.PhaseShifter),
        ):
            for branch in devices(case, element_type):
                ratings = {}
                for label, condition in (
                    ("A", pssoPy.LimitCondition.ConditionA),
                    ("B", pssoPy.LimitCondition.ConditionB),
                    ("C", pssoPy.LimitCondition.ConditionC),
                ):
                    limits = branch.GetFlowLimits(condition)
                    ratings[label] = {
                        "from_mva": float(getattr(limits, "FromLimit" + label)),
                        "to_mva": float(getattr(limits, "ToLimit" + label)),
                        "active_limit_mva": float(limits.ActiveLimit),
                        "percent_of_limit": float(limits.PercentOfLimit),
                    }
                item = {
                    "kind": kind,
                    "name": branch.Name,
                    "mrid": (branch.GetRdfID() or "").lstrip("#"),
                    "in_service": bool(branch.IsInService()),
                    "r_pu": float(branch.BranchR),
                    "x_pu": float(branch.BranchX),
                    "b_pu": float(branch.BranchB),
                    "from_section": section_data(branch.GetFromSection()),
                    "to_section": section_data(branch.GetToSection()),
                    "ratings": ratings,
                }
                if kind == "Transformer":
                    item["control"] = control_data(branch.GetControl())
                    item["secondary_control"] = control_data(branch.GetSecondaryControl())
                branches.append(item)
        banks = []
        for bank in devices(case, pssoPy.ElementType.Bank):
            banks.append(
                {
                    "name": bank.Name,
                    "mrid": (bank.GetRdfID() or "").lstrip("#"),
                    "in_service": bool(bank.IsInService()),
                    "bus": section_data(bank.GetBus()),
                    "controlled_section": section_data(bank.GetControlledSection()),
                    "control_mode": str(bank.ControlMode),
                    "is_fixed": bool(bank.IsFixed),
                    "maximum_sections": int(bank.MaximumSections),
                    "switched_on_sections": int(bank.SwitchedOnSections),
                    "mvar_per_section": float(bank.MvarPerSection),
                    "bank_mvar": float(bank.BankMvar),
                    "minimum_kv": float(bank.MinimumkV),
                    "maximum_kv": float(bank.MaximumkV),
                }
            )
        svcs = []
        for svc in devices(case, pssoPy.ElementType.SVC):
            svcs.append(
                {
                    "name": svc.Name,
                    "mrid": (svc.GetRdfID() or "").lstrip("#"),
                    "in_service": bool(svc.IsInService()),
                    "bus": section_data(svc.GetBus()),
                    "controlled_section": section_data(svc.GetControlledSection()),
                    "control_mode": str(svc.ControlMode),
                    "set_point": float(svc.SetPoint),
                    "slope": float(svc.Slope),
                    "capacitive_mvar_rating": float(svc.CapacitiveMvarRating),
                    "inductive_mvar_rating": float(svc.InductiveMvarRating),
                    "present_mvar": float(svc.PresentMvar),
                    "present_kv": float(svc.PresentkV),
                }
            )
        summary = case.GetPowerFlowSummary() if solved else None
        response.update(
            {
                "valid": True,
                "stage": "complete",
                "power_flow_converged": solved,
                "units": units,
                "buses": buses,
                "loads": loads,
                "branches": branches,
                "banks": banks,
                "svcs": svcs,
                "network_summary": {
                    "active_islands": int(case.GetNetworkSummary().ActiveIslands),
                    "buses": int(case.GetNetworkSummary().Buses),
                    "units": int(case.GetNetworkSummary().Units),
                    "lines": int(case.GetNetworkSummary().Lines),
                    "transformers": int(case.GetNetworkSummary().Transformers),
                    "banks": int(case.GetNetworkSummary().Banks),
                    "svcs": int(case.GetNetworkSummary().SVCs),
                },
                "power_flow_summary": (
                    {
                        name: float(getattr(summary, name))
                        for name in (
                            "GenerationMW", "GenerationMvar", "LoadMW", "LoadMvar",
                            "LossMW", "LossMvar", "BusShuntMW", "BusShuntMvar",
                            "LineShuntMW", "LineShuntMvar", "LargestMismatchMVA",
                            "TotalMismatchMVA", "HighestVoltage", "LowestVoltage",
                        )
                    }
                    if summary is not None
                    else None
                ),
            }
        )
    except Exception as exc:
        response.update({"error_message": str(exc), "traceback": traceback.format_exc()})
    finally:
        try:
            odmsPy.Case().CloseCase()
        except Exception:
            pass
        write_json(request["response_json"], response)


main()
