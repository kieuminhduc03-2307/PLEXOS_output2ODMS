"""Create approved synthetic identity fixtures for public native-ZIP build acceptance."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from plexos_output2odms.crosswalk.generator_dispatch import GeneratorMapping, write_crosswalk
from plexos_output2odms.plexos_solution.dispatch import SolutionSelection
from plexos_output2odms.plexos_solution.reader import list_solution_timestamps, read_dispatch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("solution_zip", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    timestamps = list_solution_timestamps(args.solution_zip)[: args.hours]
    dispatches = [read_dispatch(args.solution_zip, SolutionSelection("ST", "Interval", stamp, "Mean", None)) for stamp in timestamps]
    names = sorted({row.generator_name for rows in dispatches for row in rows})
    target = args.output / "synthetic_target.xml"
    target.write_text("<rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'/>\n", encoding="utf-8")
    mappings = [GeneratorMapping(
        source_name=name, source_guid=f"synthetic-{index}", source_object_id=index,
        source_psse_key=name, odms_synchronous_machine_mrid=f"machine-{index}",
        odms_generating_unit_mrid=f"unit-{index}", odms_machine_name=name,
        approved=True, min_operating_p_mw=0.0, max_operating_p_mw=1e9,
        source_operating_class="THERMAL", status_policy="BINARY_COMMITMENT",
        mapping_basis="synthetic exact native identity for parser/build acceptance",
    ) for index, name in enumerate(names, 1)]
    write_crosswalk(mappings, args.output / "generator_crosswalk.json", source_model=str(args.solution_zip), target_cim=str(target))
    load_crosswalk = {
        "schema": "plexos-output2odms-load-crosswalk-v1", "profile": "generic",
        "mapping_count": 1, "mappings": [{
            "source_bus_id": "SYSTEM", "source_load_id": "SYSTEM_LOAD", "source_area": "SYSTEM",
            "source_base_p_mw": 1.0, "source_base_q_mvar": 0.0,
            "odms_load_name": "SYSTEM_LOAD", "odms_load_mrid": "load-1",
            "approved": True, "mapping_basis": "synthetic system balance identity",
        }],
    }
    (args.output / "load_crosswalk.json").write_text(json.dumps(load_crosswalk, indent=2) + "\n", encoding="utf-8")
    with (args.output / "load_series.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["timestamp", "source_load_id", "p_mw", "q_mvar", "p_provenance", "q_provenance", "q_policy"])
        for stamp, rows in zip(timestamps, dispatches):
            writer.writerow([stamp.isoformat(), "SYSTEM_LOAD", sum(row.generation_mw for row in rows), 0.0, "SYNTHETIC_BALANCE_CONTEXT", "SYNTHETIC_ZERO_Q", "explicit_zero"])
    print(json.dumps({"timestamps": len(timestamps), "generators": len(names), "output": str(args.output.resolve())}, indent=2))


if __name__ == "__main__":
    main()
