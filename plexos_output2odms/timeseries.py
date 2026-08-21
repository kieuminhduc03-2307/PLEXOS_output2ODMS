from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .pipeline import SnapshotConfig, build_dispatch_snapshot, write_snapshot_outputs
from .plexos_solution.reader import list_solution_timestamps
from .time_semantics import SourceTimeContext


@dataclass(frozen=True)
class TimeSeriesConfig:
    snapshot: SnapshotConfig
    mode: str = "analysis-only"
    server: str = r".\SQLEXPRESS"
    model: str = "RTS-GMLC"
    start: datetime | None = None
    hours: int | None = None
    build_only: bool = False
    min_voltage_pu: float = 0.9
    max_voltage_pu: float = 1.1
    max_loading_percent: float = 100.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _result_row(time_data: dict, response: dict | None, error: str | None) -> dict:
    response = response or {}
    summary = response.get("power_flow_summary") or {}
    pre = response.get("preflight") or {}
    post = response.get("postflight") or {}
    gates = post.get("engineering_gates") or {}
    return {
        "source_wall_clock": time_data.get("source_wall_clock"),
        "source_time_basis": time_data.get("source_time_basis"),
        "source_timezone": time_data.get("source_timezone"),
        "analysis_timezone": time_data.get("analysis_timezone"),
        "timestamp_utc": time_data.get("timestamp_utc"),
        "valid": bool(response.get("valid")),
        "power_flow_converged": bool(response.get("power_flow_converged")),
        "generator_requested_mw": pre.get("generator_requested_mw"),
        "generator_readback_mw": pre.get("generator_readback_mw"),
        "load_requested_mw": pre.get("load_requested_mw"),
        "load_readback_mw": pre.get("load_readback_mw"),
        "load_requested_mvar": pre.get("load_requested_mvar"),
        "unit_in_service_count": pre.get("unit_in_service_count"),
        "unit_out_of_service_count": pre.get("unit_out_of_service_count"),
        "system_generation_mw": summary.get("GenerationMW"),
        "system_load_mw": summary.get("LoadMW"),
        "system_loss_mw": summary.get("LossMW"),
        "balance_residual_mw": post.get("system_active_balance_residual_mw"),
        "unattributed_swing_mw": post.get("unattributed_swing_mw"),
        "minimum_voltage_pu": gates.get("minimum_voltage_pu"),
        "maximum_voltage_pu": gates.get("maximum_voltage_pu"),
        "maximum_loading_percent": gates.get("maximum_loading_percent"),
        "engineering_gates_passed": gates.get("passed"),
        "failure": error or response.get("error_message"),
    }


def run_timeseries(
    solution: Path,
    crosswalk: Path,
    target_cim: Path,
    output: Path,
    *,
    regional_load: Path,
    load_crosswalk: Path,
    commitment: Path,
    config: TimeSeriesConfig,
) -> dict:
    if config.mode == "native-schedule":
        raise ValueError("native-schedule is a future mode; StoreSolutionState is not a native schedule")
    if config.mode not in {"analysis-only", "sv-store"}:
        raise ValueError(f"Unsupported time-series mode: {config.mode}")
    timestamps = list_solution_timestamps(solution)
    if config.start is not None:
        timestamps = [value for value in timestamps if value >= config.start]
    if config.hours is not None:
        if config.hours <= 0:
            raise ValueError("hours must be positive")
        timestamps = timestamps[: config.hours]
    if not timestamps:
        raise ValueError("No timestamps match the requested time-series window")
    output.mkdir(parents=True, exist_ok=True)
    source_files = [solution, crosswalk, target_cim, regional_load, load_crosswalk, commitment]
    manifest = {
        "schema": "plexos-output2odms-run-manifest-v1",
        "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": config.mode,
        "case_independence": "fresh ODMS.exe process and BuildCase per timestamp",
        "build_only": config.build_only,
        "source_files": [
            {"path": str(path.resolve()), "sha256": _sha256(path)} for path in source_files
        ],
        "requested_timestamp_count": len(timestamps),
        "entries": [],
    }
    rows = []
    runner = Path(__file__).resolve().parents[1] / "scripts" / "run_odms_snapshot.ps1"
    for index, timestamp in enumerate(timestamps, 1):
        stamp = timestamp.strftime("%Y%m%dT%H%M%S")
        directory = output / stamp
        result = build_dispatch_snapshot(
            solution,
            crosswalk,
            target_cim,
            timestamp=timestamp,
            config=config.snapshot,
            regional_load_path=regional_load,
            load_crosswalk_path=load_crosswalk,
            commitment_path=commitment,
        )
        context = SourceTimeContext(
            timestamp,
            config.snapshot.source_time_basis,
            config.snapshot.source_timezone,
            config.snapshot.analysis_timezone,
        )
        outputs = write_snapshot_outputs(result, directory, scenario_time=context.analysis_aware)
        entry = {
            "ordinal": index,
            "time": context.to_dict(),
            "snapshot_directory": str(directory.resolve()),
            "snapshot_valid": result.report.ok,
            "response_json": None,
        }
        response = None
        error = None
        if not result.report.ok:
            error = "snapshot validation failed"
        elif not config.build_only:
            response_path = directory / "odms_pf_result.json"
            entry["response_json"] = str(response_path.resolve())
            command = [
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner),
                "-OperatingSnapshot", outputs["operating_snapshot"],
                "-ResponseJson", str(response_path), "-Server", config.server, "-Model", config.model,
                "-MinVoltagePU", str(config.min_voltage_pu),
                "-MaxVoltagePU", str(config.max_voltage_pu),
                "-MaxLoadingPercent", str(config.max_loading_percent),
            ]
            if config.mode == "sv-store":
                command.append("-StoreSV")
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            if response_path.exists():
                response = json.loads(response_path.read_text(encoding="utf-8-sig"))
            if completed.returncode != 0:
                error = completed.stderr.strip() or f"ODMS runner exit code {completed.returncode}"
        row = _result_row(context.to_dict(), response, error)
        rows.append(row)
        entry.update({"valid": row["valid"], "failure": row["failure"]})
        manifest["entries"].append(entry)
        (output / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    fields = list(rows[0])
    with (output / "timeseries_result.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    manifest["completed_timestamp_count"] = len(rows)
    manifest["valid_timestamp_count"] = sum(bool(row["valid"]) for row in rows)
    manifest["all_valid"] = all(bool(row["valid"]) for row in rows)
    (output / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
