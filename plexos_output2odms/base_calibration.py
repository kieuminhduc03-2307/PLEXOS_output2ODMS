from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def calibrate_rts_base_ac(
    generator_data: str | Path,
    bus_data: str | Path,
    generator_crosswalk: str | Path,
    load_crosswalk: str | Path,
    branch_crosswalk: str | Path,
    odms_ac_audit: str | Path,
) -> dict:
    paths = {
        "generator_data": Path(generator_data),
        "bus_data": Path(bus_data),
        "generator_crosswalk": Path(generator_crosswalk),
        "load_crosswalk": Path(load_crosswalk),
        "branch_crosswalk": Path(branch_crosswalk),
        "odms_ac_audit": Path(odms_ac_audit),
    }
    generators = {row["GEN UID"].strip(): row for row in _csv(paths["generator_data"])}
    buses = {row["Bus ID"].strip(): row for row in _csv(paths["bus_data"])}
    generator_mappings = json.loads(paths["generator_crosswalk"].read_text(encoding="utf-8"))["mappings"]
    load_mappings = json.loads(paths["load_crosswalk"].read_text(encoding="utf-8"))["mappings"]
    branch_payload = json.loads(paths["branch_crosswalk"].read_text(encoding="utf-8"))
    audit = json.loads(paths["odms_ac_audit"].read_text(encoding="utf-8"))
    if not audit.get("valid") or not audit.get("power_flow_converged"):
        raise ValueError("ODMS base AC audit is invalid or non-converged")
    units = {row["mrid"]: row for row in audit["units"]}
    loads = {row["mrid"]: row for row in audit["loads"]}
    odms_buses = {str(row["bus_number"]): row for row in audit["buses"]}
    generator_rows = []
    for mapping in generator_mappings:
        source = generators[mapping["source_name"]]
        target = units[mapping["odms_synchronous_machine_mrid"]]
        source_kv = float(source["V Setpoint p.u."]) * float(target["bus"]["base_kv"])
        generator_rows.append(
            {
                "source": mapping["source_name"],
                "target": target["name"],
                "scheduled_p_error_mw": float(target["scheduled_mw"]) - float(source["MW Inj"]),
                "scheduled_q_reference_error_mvar": float(target["scheduled_mvar"]) - float(source["MVAR Inj"]),
                "scheduled_voltage_error_kv": float(target["scheduled_kv"]) - source_kv,
                "q_min_error_mvar": float(target["minimum_mvar"]) - float(source["QMin MVAR"]),
                "q_max_error_mvar": float(target["maximum_mvar"]) - float(source["QMax MVAR"]),
                "regulating_code": target["regulating_code"],
                "present_q_mvar": float(target["present_mvar"]),
                "base_q_reference_mvar": float(source["MVAR Inj"]),
            }
        )
    load_rows = []
    for mapping in load_mappings:
        target = loads[mapping["odms_load_mrid"]]
        load_rows.append(
            {
                "source_bus": mapping["source_bus_id"],
                "target": target["name"],
                "p_error_mw": float(target["total_mw"]) - float(mapping["source_base_p_mw"]),
                "q_error_mvar": float(target["total_mvar"]) - float(mapping["source_base_q_mvar"]),
            }
        )
    bus_rows = []
    for bus_id, source in buses.items():
        target = odms_buses[bus_id]
        bus_rows.append(
            {
                "bus_id": bus_id,
                "bus_type": source["Bus Type"],
                "source_voltage_pu": float(source["V Mag"]),
                "odms_voltage_pu": float(target["present_voltage_pu"]),
                "voltage_difference_pu": float(target["present_voltage_pu"]) - float(source["V Mag"]),
            }
        )
    absolute = lambda rows, key: max((abs(float(row[key])) for row in rows), default=0.0)
    summary = {
        "generator_count": len(generator_rows),
        "load_count": len(load_rows),
        "branch_rating_count": len(branch_payload.get("mappings", [])),
        "regulating_generator_count": sum(row["regulating_code"] == "Regulating" for row in generator_rows),
        "nonregulating_generator_count": sum(row["regulating_code"] != "Regulating" for row in generator_rows),
        "max_scheduled_p_error_mw": absolute(generator_rows, "scheduled_p_error_mw"),
        "max_scheduled_q_reference_error_mvar": absolute(generator_rows, "scheduled_q_reference_error_mvar"),
        "max_scheduled_voltage_error_kv": absolute(generator_rows, "scheduled_voltage_error_kv"),
        "max_q_min_error_mvar": absolute(generator_rows, "q_min_error_mvar"),
        "max_q_max_error_mvar": absolute(generator_rows, "q_max_error_mvar"),
        "max_load_p_error_mw": absolute(load_rows, "p_error_mw"),
        "max_load_q_error_mvar": absolute(load_rows, "q_error_mvar"),
        "max_solved_bus_voltage_difference_pu": absolute(bus_rows, "voltage_difference_pu"),
        "odms_base_pf_converged": True,
    }
    contract_passed = (
        len(generator_rows) == 158
        and len(load_rows) == 51
        and summary["branch_rating_count"] == 120
        and summary["max_scheduled_p_error_mw"] <= 1e-3
        and summary["max_scheduled_q_reference_error_mvar"] <= 1e-3
        and summary["max_scheduled_voltage_error_kv"] <= 1e-2
        and summary["max_q_min_error_mvar"] <= 1e-3
        and summary["max_q_max_error_mvar"] <= 1e-3
        and summary["max_load_p_error_mw"] <= 1e-3
        and summary["max_load_q_error_mvar"] <= 1e-3
    )
    return {
        "schema": "plexos-output2odms-rts-base-ac-calibration-v1",
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "contract_passed": contract_passed,
        "source_note": (
            "MVAR Inj is a base calibration reference only, never a PLEXOS Q(t) schedule. "
            "Solved per-unit Q may differ; ODMS solves Q subject to Vset and static limits."
        ),
        "summary": summary,
        "sources": {
            key: {"path": str(path.resolve()), "sha256": _sha256(path)}
            for key, path in paths.items()
        },
        "generator_rows": generator_rows,
        "load_rows": load_rows,
        "bus_rows": bus_rows,
        "network_controls": {
            "banks": audit.get("banks", []),
            "svcs": audit.get("svcs", []),
            "transformers": [
                {"name": row["name"], "control": row.get("control")}
                for row in audit.get("branches", [])
                if row.get("kind") == "Transformer"
            ],
        },
    }
