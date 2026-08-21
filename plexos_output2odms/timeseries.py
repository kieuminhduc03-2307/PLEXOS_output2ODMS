from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
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
    failure_policy: str = "continue-on-error"
    snapshot_timeout_seconds: float = 300.0
    max_retries: int = 0
    resume: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _result_row(
    time_data: dict,
    response: dict | None,
    error: str | None,
    *,
    fallback_outcome: str | None = None,
) -> dict:
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
        "adapter_valid": bool(response.get("adapter_valid")),
        "ac_valid": bool(response.get("ac_valid")),
        "outcome_class": response.get("outcome_class") or fallback_outcome,
        "outcome_flags": "|".join(response.get("outcome_flags") or []),
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
        "voltage_violation_count": gates.get("voltage_violation_count"),
        "generator_violation_count": gates.get("generator_violation_count"),
        "overload_count": gates.get("overload_count"),
        "rated_branch_count": gates.get("monitored_branch_count"),
        "unrated_branch_count": gates.get("unrated_branch_count"),
        "failure": error or response.get("error_message"),
    }


def _tail(value: str | None, limit: int = 4000) -> str:
    value = value or ""
    return value[-limit:]


def _execute_odms(
    command: list[str],
    response_path: Path,
    *,
    timeout_seconds: float,
    max_retries: int,
) -> tuple[dict | None, str | None, str | None, list[dict]]:
    attempts = []
    response = None
    error = None
    outcome = None
    for attempt in range(1, max_retries + 2):
        if response_path.exists():
            response_path.unlink()
        record = {"attempt": attempt, "started_utc": _utc_now()}
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds + 30.0,
            )
            record.update(
                {
                    "finished_utc": _utc_now(),
                    "return_code": completed.returncode,
                    "stdout_tail": _tail(completed.stdout),
                    "stderr_tail": _tail(completed.stderr),
                }
            )
            if completed.returncode != 0:
                error = completed.stderr.strip() or f"ODMS runner exit code {completed.returncode}"
                outcome = (
                    "EXECUTION_TIMEOUT"
                    if "timed out" in error.casefold()
                    else "EXECUTION_FAILED"
                )
            elif not response_path.exists():
                error = "ODMS runner completed without a response JSON"
                outcome = "EXECUTION_RESPONSE_MISSING"
            else:
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8-sig"))
                    error = None
                    outcome = None
                except (OSError, json.JSONDecodeError) as exc:
                    error = f"Invalid ODMS response JSON: {exc}"
                    outcome = "EXECUTION_RESPONSE_INVALID"
        except subprocess.TimeoutExpired as exc:
            record.update(
                {
                    "finished_utc": _utc_now(),
                    "timed_out": True,
                    "stdout_tail": _tail(exc.stdout if isinstance(exc.stdout, str) else None),
                    "stderr_tail": _tail(exc.stderr if isinstance(exc.stderr, str) else None),
                }
            )
            error = f"ODMS snapshot timed out after {timeout_seconds:g} seconds"
            outcome = "EXECUTION_TIMEOUT"
        attempts.append(record)
        if error is None:
            break
    return response, error, outcome, attempts


def _contract_fingerprint(source_records: list[dict], config: TimeSeriesConfig) -> tuple[str, dict]:
    contract = {
        "source_files": source_records,
        "snapshot": asdict(config.snapshot),
        "mode": config.mode,
        "server": config.server,
        "model": config.model,
        "start": config.start.isoformat() if config.start else None,
        "hours": config.hours,
        "build_only": config.build_only,
        "min_voltage_pu": config.min_voltage_pu,
        "max_voltage_pu": config.max_voltage_pu,
        "max_loading_percent": config.max_loading_percent,
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), contract


def _finish_manifest(manifest: dict, rows: list[dict], requested_count: int) -> None:
    manifest["updated_utc"] = _utc_now()
    manifest["completed_timestamp_count"] = len(rows)
    manifest["valid_timestamp_count"] = sum(bool(row["valid"]) for row in rows)
    manifest["adapter_valid_timestamp_count"] = sum(bool(row["adapter_valid"]) for row in rows)
    manifest["outcome_counts"] = {
        outcome: sum(row["outcome_class"] == outcome for row in rows)
        for outcome in sorted({row["outcome_class"] for row in rows if row["outcome_class"]})
    }
    manifest["all_valid"] = len(rows) == requested_count and all(
        bool(row["valid"]) for row in rows
    )


def run_timeseries(
    solution: Path,
    crosswalk: Path,
    target_cim: Path,
    output: Path,
    *,
    regional_load: Path,
    load_crosswalk: Path,
    commitment: Path,
    branch_crosswalk: Path | None,
    config: TimeSeriesConfig,
) -> dict:
    if config.mode == "native-schedule":
        raise ValueError("native-schedule is a future mode; StoreSolutionState is not a native schedule")
    if config.mode not in {"analysis-only", "sv-store"}:
        raise ValueError(f"Unsupported time-series mode: {config.mode}")
    if config.failure_policy not in {"fail-fast", "continue-on-error"}:
        raise ValueError(f"Unsupported failure policy: {config.failure_policy}")
    if config.snapshot_timeout_seconds <= 0:
        raise ValueError("snapshot timeout must be positive")
    if config.max_retries < 0:
        raise ValueError("max retries cannot be negative")

    timestamps = list_solution_timestamps(solution)
    if config.start is not None:
        timestamps = [value for value in timestamps if value >= config.start]
    if config.hours is not None:
        if config.hours <= 0:
            raise ValueError("hours must be positive")
        timestamps = timestamps[: config.hours]
    if not timestamps:
        raise ValueError("No timestamps match the requested time-series window")

    manifest_path = output / "run_manifest.json"
    existing_manifest = None
    if manifest_path.exists():
        if not config.resume:
            raise ValueError("Output already has a run manifest; use --resume or a new directory")
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    elif output.exists() and any(output.iterdir()) and not config.resume:
        raise ValueError("Output directory is not empty; use --resume or a new directory")
    output.mkdir(parents=True, exist_ok=True)

    source_files = [solution, crosswalk, target_cim, regional_load, load_crosswalk, commitment]
    if branch_crosswalk is not None:
        source_files.append(branch_crosswalk)
    source_records = [
        {"path": str(path.resolve()), "sha256": _sha256(path)} for path in source_files
    ]
    fingerprint, contract = _contract_fingerprint(source_records, config)
    if existing_manifest is not None and existing_manifest.get("run_fingerprint") != fingerprint:
        raise ValueError("Resume contract differs from the existing run manifest")

    previous_entries = {
        entry.get("stamp"): entry for entry in (existing_manifest or {}).get("entries", [])
    }
    manifest = {
        "schema": "plexos-output2odms-run-manifest-v2",
        "created_utc": (existing_manifest or {}).get("created_utc", _utc_now()),
        "updated_utc": _utc_now(),
        "status": "running",
        "mode": config.mode,
        "case_independence": "fresh ODMS.exe process and BuildCase per attempted timestamp",
        "build_only": config.build_only,
        "failure_policy": config.failure_policy,
        "snapshot_timeout_seconds": config.snapshot_timeout_seconds,
        "max_retries": config.max_retries,
        "resume": config.resume,
        "run_fingerprint": fingerprint,
        "contract": contract,
        "source_files": source_records,
        "requested_timestamp_count": len(timestamps),
        "entries": [],
    }
    rows = []
    infrastructure_failures = 0
    resume_skips = 0
    stopped_fail_fast = False
    runner = Path(__file__).resolve().parents[1] / "scripts" / "run_odms_snapshot.ps1"

    for index, timestamp in enumerate(timestamps, 1):
        stamp = timestamp.strftime("%Y%m%dT%H%M%S")
        previous = previous_entries.get(stamp)
        if (
            config.resume
            and previous is not None
            and previous.get("status") == "completed"
            and isinstance(previous.get("result"), dict)
        ):
            entry = dict(previous)
            entry["ordinal"] = index
            entry["resumed_skip"] = True
            manifest["entries"].append(entry)
            rows.append(entry["result"])
            resume_skips += 1
            _finish_manifest(manifest, rows, len(timestamps))
            _write_rows(output / "timeseries_result.csv", rows)
            _atomic_json(manifest_path, manifest)
            continue

        directory = output / stamp
        context = SourceTimeContext(
            timestamp,
            config.snapshot.source_time_basis,
            config.snapshot.source_timezone,
            config.snapshot.analysis_timezone,
        )
        entry = {
            "ordinal": index,
            "stamp": stamp,
            "time": context.to_dict(),
            "snapshot_directory": str(directory.resolve()),
            "status": "running",
            "started_utc": _utc_now(),
            "response_json": None,
            "attempts": [],
        }
        manifest["entries"].append(entry)
        _atomic_json(manifest_path, manifest)

        response = None
        error = None
        fallback_outcome = None
        batch_failure = False
        try:
            result = build_dispatch_snapshot(
                solution,
                crosswalk,
                target_cim,
                timestamp=timestamp,
                config=config.snapshot,
                regional_load_path=regional_load,
                load_crosswalk_path=load_crosswalk,
                commitment_path=commitment,
                branch_crosswalk_path=branch_crosswalk,
            )
            outputs = write_snapshot_outputs(result, directory, scenario_time=context.analysis_aware)
            entry["snapshot_valid"] = result.report.ok
            if not result.report.ok:
                error = "snapshot validation failed"
                fallback_outcome = "SNAPSHOT_VALIDATION_FAILED"
                batch_failure = True
            elif config.build_only:
                response = {
                    "valid": True,
                    "adapter_valid": True,
                    "ac_valid": False,
                    "outcome_class": "BUILD_ONLY_SNAPSHOT_VALID",
                    "outcome_flags": [],
                }
            else:
                response_path = directory / "odms_pf_result.json"
                entry["response_json"] = str(response_path.resolve())
                command = [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(runner),
                    "-OperatingSnapshot", outputs["operating_snapshot"],
                    "-ResponseJson", str(response_path),
                    "-Server", config.server,
                    "-Model", config.model,
                    "-MinVoltagePU", str(config.min_voltage_pu),
                    "-MaxVoltagePU", str(config.max_voltage_pu),
                    "-MaxLoadingPercent", str(config.max_loading_percent),
                    "-ProcessTimeoutSeconds", str(config.snapshot_timeout_seconds),
                ]
                if config.mode == "sv-store":
                    command.append("-StoreSV")
                response, error, fallback_outcome, attempts = _execute_odms(
                    command,
                    response_path,
                    timeout_seconds=config.snapshot_timeout_seconds,
                    max_retries=config.max_retries,
                )
                entry["attempts"] = attempts
                batch_failure = error is not None
        except Exception as exc:
            error = f"Snapshot execution failed: {exc}"
            fallback_outcome = "SNAPSHOT_BUILD_FAILED"
            batch_failure = True

        row = _result_row(context.to_dict(), response, error, fallback_outcome=fallback_outcome)
        rows.append(row)
        entry.update(
            {
                "status": "failed" if batch_failure else "completed",
                "finished_utc": _utc_now(),
                "valid": row["valid"],
                "adapter_valid": row["adapter_valid"],
                "ac_valid": row["ac_valid"],
                "outcome_class": row["outcome_class"],
                "outcome_flags": row["outcome_flags"],
                "failure": row["failure"],
                "result": row,
            }
        )
        if batch_failure:
            infrastructure_failures += 1
        _finish_manifest(manifest, rows, len(timestamps))
        manifest["infrastructure_failure_count"] = infrastructure_failures
        manifest["resume_skip_count"] = resume_skips
        _write_rows(output / "timeseries_result.csv", rows)
        _atomic_json(manifest_path, manifest)
        if batch_failure and config.failure_policy == "fail-fast":
            stopped_fail_fast = True
            break

    _finish_manifest(manifest, rows, len(timestamps))
    manifest["infrastructure_failure_count"] = infrastructure_failures
    manifest["resume_skip_count"] = resume_skips
    if stopped_fail_fast:
        manifest["status"] = "stopped_fail_fast"
    elif infrastructure_failures:
        manifest["status"] = "completed_with_failures"
    else:
        manifest["status"] = "completed"
    manifest["finished_utc"] = _utc_now()
    manifest["batch_succeeded"] = manifest["status"] == "completed"
    _write_rows(output / "timeseries_result.csv", rows)
    _atomic_json(manifest_path, manifest)
    return manifest
