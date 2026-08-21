from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .crosswalk.generator_dispatch import build_rts_gmlc_crosswalk, write_crosswalk
from .crosswalk.load_snapshot import build_rts_gmlc_load_crosswalk, write_load_crosswalk
from .odms.runtime import apply_dispatch_and_solve
from .pipeline import SnapshotConfig, build_dispatch_snapshot, write_snapshot_outputs
from .plexos_solution.reader import inspect_solution


def _timestamp(value: str, timezone_name: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo(timezone_name))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plexos-output2odms",
        description="PLEXOS optimized dispatch to ODMS scheduled operating snapshot adapter",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="Inspect a PLEXOS Solution ZIP or result table")
    inspect.add_argument("solution", type=Path)

    crosswalk = commands.add_parser("build-crosswalk", help="Build an exact RTS-GMLC Generator-to-ODMS mapping")
    crosswalk.add_argument("plexos_model", type=Path)
    crosswalk.add_argument("odms_cim", type=Path)
    crosswalk.add_argument("output", type=Path)
    crosswalk.add_argument("--profile", choices=["rts-gmlc"], default="rts-gmlc")
    crosswalk.add_argument("--approve", action="store_true", help="Explicitly approve generated exact mappings")

    load_crosswalk = commands.add_parser(
        "build-load-crosswalk", help="Build an exact RTS-GMLC bus-load-to-ODMS mapping"
    )
    load_crosswalk.add_argument("bus_data", type=Path)
    load_crosswalk.add_argument("odms_cim", type=Path)
    load_crosswalk.add_argument("output", type=Path)
    load_crosswalk.add_argument("--profile", choices=["rts-gmlc"], default="rts-gmlc")
    load_crosswalk.add_argument("--approve", action="store_true")

    snapshot = commands.add_parser("build-snapshot", help="Build one validated dispatch snapshot")
    snapshot.add_argument("solution", type=Path)
    snapshot.add_argument("crosswalk", type=Path)
    snapshot.add_argument("target_cim", type=Path)
    snapshot.add_argument("output_directory", type=Path)
    snapshot.add_argument("--timestamp", required=True)
    snapshot.add_argument("--timezone", required=True, help="IANA timezone for timestamps without an offset")
    snapshot.add_argument("--phase", default="ST")
    snapshot.add_argument("--period", default="Interval")
    snapshot.add_argument("--sample", default="Mean")
    snapshot.add_argument("--unit", default=None, help="Required for wide files, normally MW")
    snapshot.add_argument("--dependent-on", default=None, help="Authoritative target EQ FullModel URI")
    snapshot.add_argument("--regional-load", type=Path, default=None)
    snapshot.add_argument("--load-crosswalk", type=Path, default=None)
    snapshot.add_argument("--commitment", type=Path, default=None)
    snapshot.add_argument("--balance-tolerance-mw", type=float, default=1e-6)
    snapshot.add_argument(
        "--missing-dispatch",
        choices=["error", "preserve"],
        default="error",
        help="Fail by default; preserve leaves ODMS ScheduledMW unchanged for absent generators",
    )

    run = commands.add_parser("run-odms", help="Apply a complete operating snapshot and solve PF")
    run.add_argument("operating_snapshot", type=Path)
    run.add_argument("result_json", type=Path)
    run.add_argument("--mode", choices=["direct", "ssh"], default="direct")
    run.add_argument("--ssh", type=Path, default=None)
    run.add_argument("--store-sv", action="store_true", help="Persist solved SV only after convergence")
    return parser


def _read_normalized(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8", newline="") as stream:
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
    if not rows:
        raise ValueError("Normalized dispatch CSV is empty")
    return rows


def _read_operating_snapshot(
    path: Path,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    if path.suffix.lower() == ".csv":
        return _read_normalized(path), [], [], []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "plexos-output2odms-operating-snapshot-v1":
        raise ValueError("Unsupported operating snapshot schema")
    generators = payload.get("generator_setpoints", [])
    loads = payload.get("load_setpoints", [])
    statuses = payload.get("unit_statuses", [])
    audit_units = payload.get("audit_units", [])
    if not generators:
        raise ValueError("Operating snapshot contains no generator setpoints")
    return generators, loads, statuses, audit_units


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            print(json.dumps(inspect_solution(args.solution), indent=2))
            return 0
        if args.command == "build-crosswalk":
            mappings = build_rts_gmlc_crosswalk(
                args.plexos_model, args.odms_cim, approved=args.approve
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_crosswalk(
                mappings,
                args.output,
                source_model=str(args.plexos_model.resolve()),
                target_cim=str(args.odms_cim.resolve()),
            )
            print(f"Crosswalk: {args.output}")
            print(f"Mappings:  {len(mappings)} ({'approved' if args.approve else 'review required'})")
            return 0
        if args.command == "build-load-crosswalk":
            mappings = build_rts_gmlc_load_crosswalk(
                args.bus_data, args.odms_cim, approved=args.approve
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_load_crosswalk(
                mappings,
                args.output,
                source_bus_data=str(args.bus_data.resolve()),
                target_cim=str(args.odms_cim.resolve()),
            )
            print(f"Load crosswalk: {args.output}")
            print(f"Mappings:       {len(mappings)} ({'approved' if args.approve else 'review required'})")
            return 0
        if args.command == "build-snapshot":
            timestamp = _timestamp(args.timestamp, args.timezone)
            result = build_dispatch_snapshot(
                args.solution,
                args.crosswalk,
                args.target_cim,
                timestamp=timestamp,
                config=SnapshotConfig(
                    phase=args.phase,
                    period=args.period,
                    sample=args.sample,
                    unit=args.unit,
                    missing_dispatch_policy=args.missing_dispatch,
                    preflight_balance_tolerance_mw=args.balance_tolerance_mw,
                ),
                dependent_on=args.dependent_on,
                regional_load_path=args.regional_load,
                load_crosswalk_path=args.load_crosswalk,
                commitment_path=args.commitment,
            )
            outputs = write_snapshot_outputs(result, args.output_directory, scenario_time=timestamp)
            print(result.report.format_text())
            print(json.dumps(outputs, indent=2))
            return 0 if result.report.ok else 2
        if args.command == "run-odms":
            rows, load_rows, statuses, audit_units = _read_operating_snapshot(
                args.operating_snapshot
            )
            result = apply_dispatch_and_solve(
                rows,
                load_rows=load_rows,
                status_rows=statuses,
                audit_unit_rows=audit_units,
                ssh_path=args.ssh,
                use_ssh=args.mode == "ssh",
                store_sv=args.store_sv,
            )
            payload = {
                "converged": result.converged,
                "sv_stored": result.sv_stored,
                "power_flow_summary": result.power_flow_summary,
                "rows": result.rows,
                "load_rows": result.load_rows,
                "status_rows": result.status_rows,
                "audit_unit_rows": result.audit_unit_rows,
                "preflight": result.preflight,
                "postflight": result.postflight,
            }
            args.result_json.parent.mkdir(parents=True, exist_ok=True)
            args.result_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            print(f"Power flow converged: {result.converged}")
            print(f"Result: {args.result_json}")
            return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
