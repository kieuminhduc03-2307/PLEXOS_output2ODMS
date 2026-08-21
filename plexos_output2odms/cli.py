from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from .crosswalk.generator_dispatch import build_rts_gmlc_crosswalk, write_crosswalk
from .crosswalk.load_snapshot import build_rts_gmlc_load_crosswalk, write_load_crosswalk
from .crosswalk.branch_ratings import build_rts_branch_rating_crosswalk, write_branch_rating_crosswalk
from .odms.runtime import apply_dispatch_and_solve
from .pipeline import SnapshotConfig, build_dispatch_snapshot, write_snapshot_outputs
from .plexos_solution.reader import inspect_solution
from .time_semantics import SourceTimeContext, parse_source_wall_clock
from .timeseries import TimeSeriesConfig, run_timeseries
from .base_calibration import calibrate_rts_base_ac


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
    crosswalk.add_argument("--generator-data", type=Path, default=None, help="RTS SourceData/gen.csv AC capability contract")

    load_crosswalk = commands.add_parser(
        "build-load-crosswalk", help="Build an exact RTS-GMLC bus-load-to-ODMS mapping"
    )
    load_crosswalk.add_argument("bus_data", type=Path)
    load_crosswalk.add_argument("odms_cim", type=Path)
    load_crosswalk.add_argument("output", type=Path)
    load_crosswalk.add_argument("--profile", choices=["rts-gmlc"], default="rts-gmlc")
    load_crosswalk.add_argument("--approve", action="store_true")

    branch_crosswalk = commands.add_parser(
        "build-branch-crosswalk", help="Build RTS branch rating-to-ODMS Condition A/B/C mapping"
    )
    branch_crosswalk.add_argument("branch_data", type=Path)
    branch_crosswalk.add_argument("odms_ac_audit", type=Path)
    branch_crosswalk.add_argument("output", type=Path)
    branch_crosswalk.add_argument("--raw-reference", type=Path, default=None)
    branch_crosswalk.add_argument("--approve", action="store_true")

    calibration = commands.add_parser("calibrate-base-ac", help="Compare official RTS base AC data with a real ODMS base-case audit")
    calibration.add_argument("generator_data", type=Path)
    calibration.add_argument("bus_data", type=Path)
    calibration.add_argument("generator_crosswalk", type=Path)
    calibration.add_argument("load_crosswalk", type=Path)
    calibration.add_argument("branch_crosswalk", type=Path)
    calibration.add_argument("odms_ac_audit", type=Path)
    calibration.add_argument("output", type=Path)

    snapshot = commands.add_parser("build-snapshot", help="Build one validated dispatch snapshot")
    snapshot.add_argument("solution", type=Path)
    snapshot.add_argument("crosswalk", type=Path)
    snapshot.add_argument("target_cim", type=Path)
    snapshot.add_argument("output_directory", type=Path)
    snapshot.add_argument("--timestamp", required=True)
    snapshot.add_argument(
        "--source-time-basis",
        choices=["unknown_local", "utc", "iana_timezone"],
        default="unknown_local",
    )
    snapshot.add_argument("--source-timezone", default=None)
    snapshot.add_argument(
        "--analysis-timezone",
        "--timezone",
        dest="analysis_timezone",
        required=True,
        help="IANA timezone used only for ODMS analysis embedding; does not assert source timezone",
    )
    snapshot.add_argument("--phase", default="ST")
    snapshot.add_argument("--period", default="Interval")
    snapshot.add_argument("--sample", default="Mean")
    snapshot.add_argument("--unit", default=None, help="Required for wide files, normally MW")
    snapshot.add_argument("--dependent-on", default=None, help="Authoritative target EQ FullModel URI")
    snapshot.add_argument("--regional-load", type=Path, default=None)
    snapshot.add_argument("--load-crosswalk", type=Path, default=None)
    snapshot.add_argument("--commitment", type=Path, default=None)
    snapshot.add_argument("--branch-crosswalk", type=Path, default=None)
    snapshot.add_argument("--status-mode", choices=["crosswalk_commitment", "dispatch_on_only", "preserve_odms"], default="crosswalk_commitment")
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

    series = commands.add_parser(
        "run-timeseries", help="Build and solve independent ODMS cases for a PLEXOS time window"
    )
    series.add_argument("solution", type=Path)
    series.add_argument("crosswalk", type=Path)
    series.add_argument("target_cim", type=Path)
    series.add_argument("regional_load", type=Path)
    series.add_argument("load_crosswalk", type=Path)
    series.add_argument("commitment", type=Path)
    series.add_argument("output_directory", type=Path)
    series.add_argument("--start", default=None)
    series.add_argument("--hours", type=int, default=None)
    series.add_argument("--unit", default="MW")
    series.add_argument("--source-time-basis", choices=["unknown_local", "utc", "iana_timezone"], default="unknown_local")
    series.add_argument("--source-timezone", default=None)
    series.add_argument("--analysis-timezone", required=True)
    series.add_argument("--mode", choices=["analysis-only", "sv-store", "native-schedule"], default="analysis-only")
    series.add_argument("--status-mode", choices=["crosswalk_commitment", "dispatch_on_only", "preserve_odms"], default="crosswalk_commitment")
    series.add_argument("--server", default=r".\SQLEXPRESS")
    series.add_argument("--model", default="RTS-GMLC")
    series.add_argument("--build-only", action="store_true")
    series.add_argument("--balance-tolerance-mw", type=float, default=1e-6)
    series.add_argument(
        "--missing-dispatch", choices=["error", "preserve"], default="preserve",
        help="Preserve approved resources absent from dispatch (RTS CSP/storage are deferred)",
    )
    series.add_argument("--min-voltage-pu", type=float, default=0.9)
    series.add_argument("--max-voltage-pu", type=float, default=1.1)
    series.add_argument("--max-loading-percent", type=float, default=100.0)
    series.add_argument("--branch-crosswalk", type=Path, default=None)
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
    if payload.get("schema") not in {
        "plexos-output2odms-operating-snapshot-v1",
        "plexos-output2odms-operating-snapshot-v2",
        "plexos-output2odms-operating-snapshot-v3",
    }:
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
                args.plexos_model,
                args.odms_cim,
                approved=args.approve,
                generator_data=args.generator_data,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_crosswalk(
                mappings,
                args.output,
                source_model=str(args.plexos_model.resolve()),
                target_cim=str(args.odms_cim.resolve()),
                generator_data=str(args.generator_data.resolve()) if args.generator_data else None,
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
        if args.command == "build-branch-crosswalk":
            mappings = build_rts_branch_rating_crosswalk(
                args.branch_data, args.odms_ac_audit, approved=args.approve
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            write_branch_rating_crosswalk(
                mappings,
                args.output,
                source_branch_data=str(args.branch_data.resolve()),
                odms_ac_audit=str(args.odms_ac_audit.resolve()),
                raw_reference=str(args.raw_reference.resolve()) if args.raw_reference else None,
            )
            print(f"Branch rating crosswalk: {args.output}")
            print(f"Mappings: {len(mappings)} ({'approved' if args.approve else 'review required'})")
            return 0
        if args.command == "calibrate-base-ac":
            report = calibrate_rts_base_ac(
                args.generator_data,
                args.bus_data,
                args.generator_crosswalk,
                args.load_crosswalk,
                args.branch_crosswalk,
                args.odms_ac_audit,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report["summary"], indent=2))
            print(f"Contract passed: {report['contract_passed']}")
            print(f"Report: {args.output}")
            return 0 if report["contract_passed"] else 2
        if args.command == "build-snapshot":
            timestamp = parse_source_wall_clock(args.timestamp)
            time_context = SourceTimeContext(
                timestamp,
                source_time_basis=args.source_time_basis,
                source_timezone=args.source_timezone,
                analysis_timezone=args.analysis_timezone,
            )
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
                    source_time_basis=args.source_time_basis,
                    source_timezone=args.source_timezone,
                    analysis_timezone=args.analysis_timezone,
                    status_mode=args.status_mode,
                ),
                dependent_on=args.dependent_on,
                regional_load_path=args.regional_load,
                load_crosswalk_path=args.load_crosswalk,
                commitment_path=args.commitment,
                branch_crosswalk_path=args.branch_crosswalk,
            )
            outputs = write_snapshot_outputs(
                result,
                args.output_directory,
                scenario_time=time_context.analysis_aware,
            )
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
        if args.command == "run-timeseries":
            start = parse_source_wall_clock(args.start) if args.start else None
            snapshot_config = SnapshotConfig(
                unit=args.unit,
                missing_dispatch_policy=args.missing_dispatch,
                preflight_balance_tolerance_mw=args.balance_tolerance_mw,
                source_time_basis=args.source_time_basis,
                source_timezone=args.source_timezone,
                analysis_timezone=args.analysis_timezone,
                status_mode=args.status_mode,
            )
            manifest = run_timeseries(
                args.solution,
                args.crosswalk,
                args.target_cim,
                args.output_directory,
                regional_load=args.regional_load,
                load_crosswalk=args.load_crosswalk,
                commitment=args.commitment,
                branch_crosswalk=args.branch_crosswalk,
                config=TimeSeriesConfig(
                    snapshot=snapshot_config,
                    mode=args.mode,
                    server=args.server,
                    model=args.model,
                    start=start,
                    hours=args.hours,
                    build_only=args.build_only,
                    min_voltage_pu=args.min_voltage_pu,
                    max_voltage_pu=args.max_voltage_pu,
                    max_loading_percent=args.max_loading_percent,
                ),
            )
            print(
                json.dumps(
                    {
                        "mode": manifest["mode"],
                        "requested_timestamp_count": manifest["requested_timestamp_count"],
                        "completed_timestamp_count": manifest["completed_timestamp_count"],
                        "valid_timestamp_count": manifest["valid_timestamp_count"],
                        "adapter_valid_timestamp_count": manifest["adapter_valid_timestamp_count"],
                        "outcome_counts": manifest["outcome_counts"],
                        "all_valid": manifest["all_valid"],
                        "run_manifest": str((args.output_directory / "run_manifest.json").resolve()),
                        "timeseries_result": str((args.output_directory / "timeseries_result.csv").resolve()),
                    },
                    indent=2,
                )
            )
            return 0 if manifest["all_valid"] or args.build_only else 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
