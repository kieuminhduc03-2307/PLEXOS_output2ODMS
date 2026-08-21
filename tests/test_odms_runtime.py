from __future__ import annotations

import pytest

from plexos_output2odms.odms.runtime import apply_dispatch_and_solve


class FakeUnit:
    def __init__(self, name: str, rdf_id: str, events=None):
        self.name = name
        self.rdf_id = rdf_id
        self.ScheduledMW = 1.0
        self.ScheduledMvar = 2.0
        self.PresentMW = 0.0
        self.PresentMvar = 0.0
        self.PresentkV = 230.0
        self.events = events if events is not None else []
        self._in_service = True

    def IsNull(self): return False
    def IsError(self): return False
    def GetRdfID(self): return self.rdf_id
    def SetGeneration(self, mw, mvar):
        self.events.append("generator")
        self.ScheduledMW = mw
        self.ScheduledMvar = mvar
        return True
    def IsInService(self): return self._in_service
    def SetDeviceStatus(self, status):
        self.events.append("status")
        self._in_service = status == 0
        return True
    def Init(self): return True


class FakeLoad:
    def __init__(self, name: str, rdf_id: str, events):
        self.name = name
        self.rdf_id = rdf_id
        self.TotalMW = 1.0
        self.TotalMvar = 0.2
        self.events = events

    def IsNull(self): return False
    def IsError(self): return False
    def GetRdfID(self): return self.rdf_id
    def SetLoad(self, mw, mvar):
        self.events.append("load")
        self.TotalMW = mw
        self.TotalMvar = mvar
        return True


class FakeAnalysisCase:
    def __init__(self, unit, load=None, events=None):
        self.unit = unit
        self.load = load
        self.events = events if events is not None else []
    def GetUnit(self, name): return self.unit if name == self.unit.name else None
    def GetLoad(self, name): return self.load if self.load is not None and name == self.load.name else None
    def SolvePowerFlow(self):
        self.events.append("solve")
        self.unit.PresentMW = self.unit.ScheduledMW + 0.1
        return True
    def IsPowerFlowValid(self): return True
    def GetPowerFlowSummaryDict(self): return [{"status": "ok"}]
    def GetPowerFlowSummary(self): return FakeSummary()


class FakeSummary:
    GenerationMW = 76.1
    GenerationMvar = 0.0
    LoadMW = 76.0
    LoadMvar = 15.2
    LossMW = 0.1
    LossMvar = 0.0
    BusShuntMW = 0.0
    BusShuntMvar = 0.0
    LineShuntMW = 0.0
    LineShuntMvar = 0.0
    LargestMismatchMVA = 0.0
    TotalMismatchMVA = 0.0
    HighestVoltage = 230.0
    LowestVoltage = 230.0


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
    class DeviceStatus:
        InService = 0
        OutOfService = 1
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


def test_runtime_verifies_then_applies_load_before_generator_and_preflight():
    events = []
    unit = FakeUnit("G1", "machine-1", events)
    load = FakeLoad("101_1", "load-101", events)
    analysis = FakeAnalysisCase(unit, load, events)
    result = apply_dispatch_and_solve(
        [{"target_machine_name": "G1", "target_machine_mrid": "machine-1", "generation_mw": 76.0}],
        load_rows=[{
            "target_load_name": "101_1",
            "target_load_mrid": "load-101",
            "load_p_mw": 76.0,
            "load_q_mvar": 15.2,
        }],
        module_loader=lambda: (FakeODMS(), FakePSSO(analysis)),
    )
    assert events == ["load", "generator", "solve"]
    assert result.preflight["active_power_imbalance_mw"] == 0.0
    assert result.load_rows[0]["initialized_load_q_mvar"] == 15.2


def test_runtime_applies_status_between_load_and_generation():
    events = []
    unit = FakeUnit("G1", "machine-1", events)
    load = FakeLoad("101_1", "load-101", events)
    analysis = FakeAnalysisCase(unit, load, events)
    result = apply_dispatch_and_solve(
        [{"target_machine_name": "G1", "target_machine_mrid": "machine-1", "generation_mw": 76.0}],
        load_rows=[{
            "target_load_name": "101_1", "target_load_mrid": "load-101",
            "load_p_mw": 76.0, "load_q_mvar": 15.2,
        }],
        status_rows=[{
            "target_machine_name": "G1", "target_machine_mrid": "machine-1",
            "action": "set", "requested_in_service": False,
        }],
        module_loader=lambda: (FakeODMS(), FakePSSO(analysis)),
    )
    assert events == ["load", "status", "generator", "solve"]
    assert result.status_rows[0]["initialized_in_service"] is False


def test_runtime_rejects_identity_mismatch():
    unit = FakeUnit("G1", "wrong")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        apply_dispatch_and_solve(
            [{"target_machine_name": "G1", "target_machine_mrid": "expected", "generation_mw": 1.0}],
            module_loader=lambda: (FakeODMS(), FakePSSO(FakeAnalysisCase(unit))),
        )
