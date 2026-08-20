from __future__ import annotations

import pytest

from plexos_output2odms.odms.runtime import apply_dispatch_and_solve


class FakeUnit:
    def __init__(self, name: str, rdf_id: str):
        self.name = name
        self.rdf_id = rdf_id
        self.ScheduledMW = 1.0
        self.ScheduledMvar = 2.0
        self.PresentMW = 0.0
        self.PresentMvar = 0.0
        self.PresentkV = 230.0

    def IsNull(self): return False
    def IsError(self): return False
    def GetRdfID(self): return self.rdf_id
    def SetGeneration(self, mw, mvar):
        self.ScheduledMW = mw
        self.ScheduledMvar = mvar
        return True


class FakeAnalysisCase:
    def __init__(self, unit): self.unit = unit
    def GetUnit(self, name): return self.unit if name == self.unit.name else None
    def SolvePowerFlow(self):
        self.unit.PresentMW = self.unit.ScheduledMW + 0.1
        return True
    def IsPowerFlowValid(self): return True
    def GetPowerFlowSummaryDict(self): return [{"status": "ok"}]


class FakeODMSCase:
    def __init__(self): self.stored = False
    def BuildCase(self): return True
    def StoreSolutionState(self): self.stored = True; return True


class FakeODMS:
    def __init__(self): self.case = FakeODMSCase()
    def Case(self): return self.case
    def ClearErrors(self): pass
    def GetErrors(self): return ""


class FakePSSO:
    def __init__(self, case): self.case = case
    def GetCase(self): return self.case
    def GetLastError(self): return ""


def test_direct_runtime_sets_scheduled_mw_then_solves_and_stores_sv():
    unit = FakeUnit("G1", "machine-1")
    analysis = FakeAnalysisCase(unit)
    odms = FakeODMS()
    result = apply_dispatch_and_solve(
        [{"target_machine_name": "G1", "target_machine_mrid": "machine-1", "generation_mw": 76.0}],
        store_sv=True,
        module_loader=lambda: (odms, FakePSSO(analysis)),
    )
    assert result.converged
    assert result.sv_stored
    assert result.rows[0]["initialized_scheduled_mw"] == 76.0
    assert result.rows[0]["present_mw"] == 76.1


def test_runtime_rejects_identity_mismatch():
    unit = FakeUnit("G1", "wrong")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        apply_dispatch_and_solve(
            [{"target_machine_name": "G1", "target_machine_mrid": "expected", "generation_mw": 1.0}],
            module_loader=lambda: (FakeODMS(), FakePSSO(FakeAnalysisCase(unit))),
        )
