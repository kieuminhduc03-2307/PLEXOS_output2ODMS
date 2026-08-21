from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import pytest

import plexos_output2odms.timeseries as module
from plexos_output2odms.pipeline import SnapshotConfig
from plexos_output2odms.timeseries import TimeSeriesConfig, _execute_odms, run_timeseries


def test_odms_execution_retries_then_succeeds(tmp_path: Path, monkeypatch):
    response_path = tmp_path / "response.json"
    calls = 0

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(args[0], 1, "", "injected failure")
        response_path.write_text(
            json.dumps({"valid": True, "adapter_valid": True}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(args[0], 0, "ok", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    response, error, outcome, attempts = _execute_odms(
        ["runner"], response_path, timeout_seconds=1.0, max_retries=1
    )
    assert response["adapter_valid"] is True
    assert error is None
    assert outcome is None
    assert len(attempts) == 2


def test_odms_execution_timeout_is_bounded_and_classified(tmp_path: Path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    response, error, outcome, attempts = _execute_odms(
        ["runner"], tmp_path / "response.json", timeout_seconds=0.1, max_retries=1
    )
    assert response is None
    assert "timed out" in error
    assert outcome == "EXECUTION_TIMEOUT"
    assert len(attempts) == 2
    assert all(item["timed_out"] for item in attempts)


def test_wrapper_reported_timeout_is_classified_as_timeout(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", "ODMS process timed out after 1 seconds"
        ),
    )
    _, error, outcome, _ = _execute_odms(
        ["runner"], tmp_path / "response.json", timeout_seconds=1.0, max_retries=0
    )
    assert "timed out" in error
    assert outcome == "EXECUTION_TIMEOUT"


class _Report:
    ok = True


class _Snapshot:
    report = _Report()


def _inputs(tmp_path: Path) -> tuple[Path, ...]:
    paths = tuple(tmp_path / name for name in (
        "solution.csv", "generator.json", "target.xml", "load.csv",
        "load-crosswalk.json", "commitment.csv", "branch.json",
    ))
    for path in paths:
        path.write_text(path.name, encoding="utf-8")
    return paths


def _patch_campaign(monkeypatch, timestamps, failing=None):
    monkeypatch.setattr(module, "list_solution_timestamps", lambda _: timestamps)

    def build(*args, timestamp, **kwargs):
        if failing is not None and timestamp == failing:
            raise RuntimeError("injected snapshot failure")
        return _Snapshot()

    monkeypatch.setattr(module, "build_dispatch_snapshot", build)

    def write_outputs(result, directory, **kwargs):
        directory.mkdir(parents=True, exist_ok=True)
        operating = directory / "operating_snapshot.json"
        operating.write_text("{}", encoding="utf-8")
        return {"operating_snapshot": str(operating)}

    monkeypatch.setattr(module, "write_snapshot_outputs", write_outputs)


def _run(tmp_path: Path, monkeypatch, *, failure_policy="continue-on-error", resume=False):
    timestamps = [datetime(2020, 7, 5) + timedelta(hours=value) for value in range(3)]
    _patch_campaign(monkeypatch, timestamps, failing=timestamps[0] if not resume else None)
    solution, crosswalk, target, load, load_crosswalk, commitment, branch = _inputs(tmp_path)
    return run_timeseries(
        solution,
        crosswalk,
        target,
        tmp_path / "out",
        regional_load=load,
        load_crosswalk=load_crosswalk,
        commitment=commitment,
        branch_crosswalk=branch,
        config=TimeSeriesConfig(
            snapshot=SnapshotConfig(unit="MW", analysis_timezone="UTC"),
            build_only=True,
            failure_policy=failure_policy,
            resume=resume,
        ),
    )


def test_continue_on_error_records_failure_and_completes_remaining(tmp_path: Path, monkeypatch):
    manifest = _run(tmp_path, monkeypatch)
    assert manifest["status"] == "completed_with_failures"
    assert manifest["completed_timestamp_count"] == 3
    assert manifest["infrastructure_failure_count"] == 1
    assert [entry["status"] for entry in manifest["entries"]] == [
        "failed", "completed", "completed"
    ]


def test_fail_fast_stops_after_first_infrastructure_failure(tmp_path: Path, monkeypatch):
    manifest = _run(tmp_path, monkeypatch, failure_policy="fail-fast")
    assert manifest["status"] == "stopped_fail_fast"
    assert manifest["completed_timestamp_count"] == 1
    assert len(manifest["entries"]) == 1


def test_continue_on_error_advances_after_injected_odms_timeout(tmp_path: Path, monkeypatch):
    timestamps = [datetime(2020, 7, 5) + timedelta(hours=value) for value in range(3)]
    _patch_campaign(monkeypatch, timestamps)
    solution, crosswalk, target, load, load_crosswalk, commitment, branch = _inputs(tmp_path)
    calls = 0

    def fake_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        response_path = Path(command[command.index("-ResponseJson") + 1])
        response_path.write_text(
            json.dumps(
                {
                    "valid": True,
                    "adapter_valid": True,
                    "ac_valid": True,
                    "outcome_class": "ADAPTER_VALID_AC_VALID",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    manifest = run_timeseries(
        solution,
        crosswalk,
        target,
        tmp_path / "timeout-out",
        regional_load=load,
        load_crosswalk=load_crosswalk,
        commitment=commitment,
        branch_crosswalk=branch,
        config=TimeSeriesConfig(
            snapshot=SnapshotConfig(unit="MW", analysis_timezone="UTC"),
            failure_policy="continue-on-error",
            snapshot_timeout_seconds=0.1,
        ),
    )
    assert calls == 3
    assert manifest["status"] == "completed_with_failures"
    assert manifest["infrastructure_failure_count"] == 1
    assert manifest["entries"][0]["outcome_class"] == "EXECUTION_TIMEOUT"
    assert [entry["status"] for entry in manifest["entries"]] == [
        "failed", "completed", "completed"
    ]


def test_resume_skips_completed_entries_and_retries_failed_entry(tmp_path: Path, monkeypatch):
    first = _run(tmp_path, monkeypatch)
    assert first["infrastructure_failure_count"] == 1

    timestamps = [datetime(2020, 7, 5) + timedelta(hours=value) for value in range(3)]
    calls = []
    monkeypatch.setattr(module, "list_solution_timestamps", lambda _: timestamps)

    def build(*args, timestamp, **kwargs):
        calls.append(timestamp)
        return _Snapshot()

    monkeypatch.setattr(module, "build_dispatch_snapshot", build)
    solution, crosswalk, target, load, load_crosswalk, commitment, branch = _inputs(tmp_path)
    resumed = run_timeseries(
        solution,
        crosswalk,
        target,
        tmp_path / "out",
        regional_load=load,
        load_crosswalk=load_crosswalk,
        commitment=commitment,
        branch_crosswalk=branch,
        config=TimeSeriesConfig(
            snapshot=SnapshotConfig(unit="MW", analysis_timezone="UTC"),
            build_only=True,
            resume=True,
        ),
    )
    assert calls == [timestamps[0]]
    assert resumed["status"] == "completed"
    assert resumed["resume_skip_count"] == 2
    assert resumed["infrastructure_failure_count"] == 0


def test_resume_rejects_changed_electrical_contract(tmp_path: Path, monkeypatch):
    _run(tmp_path, monkeypatch)
    timestamps = [datetime(2020, 7, 5) + timedelta(hours=value) for value in range(3)]
    _patch_campaign(monkeypatch, timestamps)
    solution, crosswalk, target, load, load_crosswalk, commitment, branch = _inputs(tmp_path)
    with pytest.raises(ValueError, match="contract differs"):
        run_timeseries(
            solution,
            crosswalk,
            target,
            tmp_path / "out",
            regional_load=load,
            load_crosswalk=load_crosswalk,
            commitment=commitment,
            branch_crosswalk=branch,
            config=TimeSeriesConfig(
                snapshot=SnapshotConfig(unit="MW", analysis_timezone="UTC"),
                build_only=True,
                max_voltage_pu=1.2,
                resume=True,
            ),
        )
