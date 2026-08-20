from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class ODMSRunResult:
    converged: bool
    rows: list[dict]
    power_flow_summary: dict | None
    sv_stored: bool


def _load_modules():
    try:
        import odmsPy  # type: ignore
        import pssoPy  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "odmsPy/pssoPy are unavailable. Run this command inside the licensed PSS ODMS Python environment."
        ) from exc
    return odmsPy, pssoPy


def apply_dispatch_and_solve(
    rows: list[dict],
    *,
    ssh_path: str | Path | None = None,
    use_ssh: bool = False,
    store_sv: bool = False,
    module_loader: Callable = _load_modules,
) -> ODMSRunResult:
    """Apply one timestamp to the current ODMS model's in-memory case and solve PF.

    Direct application writes pssoPy.Unit.ScheduledMW. SSH mode loads the generated
    operational-state file through odmsPy.Case.LoadSSHFile. Neither mode edits EQ data.
    """
    odmsPy, pssoPy = module_loader()
    odms_case = odmsPy.Case()
    odmsPy.ClearErrors()
    if not odms_case.BuildCase():
        raise RuntimeError("ODMS BuildCase failed: " + odmsPy.GetErrors())
    case = pssoPy.GetCase()
    initialized: list[dict] = []
    if use_ssh:
        if ssh_path is None:
            raise ValueError("ssh_path is required when use_ssh=True")
        if not odms_case.LoadSSHFile(str(Path(ssh_path).resolve()), 2020):
            raise RuntimeError("ODMS LoadSSHFile failed: " + odmsPy.GetErrors())
    else:
        for row in rows:
            unit = case.GetUnit(row["target_machine_name"])
            if unit is None or unit.IsNull() or unit.IsError():
                raise RuntimeError(f"ODMS Unit not found: {row['target_machine_name']}")
            actual_rdf = (unit.GetRdfID() or "").lstrip("#")
            expected_rdf = row["target_machine_mrid"].lstrip("#")
            if actual_rdf != expected_rdf:
                raise RuntimeError(
                    f"ODMS Unit identity mismatch for {row['target_machine_name']}: "
                    f"runtime {actual_rdf!r}, crosswalk {expected_rdf!r}"
                )
            scheduled_mvar = float(unit.ScheduledMvar)
            if not unit.SetGeneration(float(row["generation_mw"]), scheduled_mvar):
                raise RuntimeError(f"SetGeneration failed for {row['target_machine_name']}")
            initialized.append(
                {
                    **row,
                    "initialized_scheduled_mw": float(unit.ScheduledMW),
                    "initialized_scheduled_mvar": float(unit.ScheduledMvar),
                }
            )
    converged = bool(case.SolvePowerFlow() and case.IsPowerFlowValid())
    if not converged:
        raise RuntimeError("ODMS Power Flow did not converge: " + pssoPy.GetLastError())
    solved = []
    for row in rows:
        unit = case.GetUnit(row["target_machine_name"])
        solved.append(
            {
                **row,
                "initialized_scheduled_mw": float(unit.ScheduledMW),
                "present_mw": float(unit.PresentMW),
                "present_mvar": float(unit.PresentMvar),
                "present_kv": float(unit.PresentkV),
            }
        )
    sv_stored = False
    if store_sv:
        if not odms_case.StoreSolutionState():
            raise RuntimeError("ODMS StoreSolutionState failed: " + odmsPy.GetErrors())
        sv_stored = True
    summary = None
    try:
        summary = case.GetPowerFlowSummaryDict()[0]
    except Exception:
        pass
    return ODMSRunResult(True, solved, summary, sv_stored)
