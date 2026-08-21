# Architecture and semantic boundary

## Two operational representations

The authoritative execution path is:

```text
OperatingSnapshot(t)
  → verify all load/unit mRIDs
  → Load.SetLoad(P,Q)
  → class-aware Unit.SetDeviceStatus()
  → Unit.SetGeneration(P,Q)
  → preflight → PF → PowerFlowSummary/SV
```

The interoperability path is:

```text
PLEXOS Generator.Generation(t) → CIM SSH SynchronousMachine/RotatingMachine.p
```

The current ODMS CIM17 export proves its load-sign convention: generated active
power is negative in `RotatingMachine.p`. Therefore a positive 76 MW PLEXOS
dispatch is represented as `RotatingMachine.p = -76` in SSH, while the ODMS case
API receives `ScheduledMW = +76`.

## Identity

Automatic name-only merge is prohibited. The approved crosswalk records:

- PLEXOS object ID, GUID, exact name, Node and capacity;
- source PSS/E reconciliation key;
- target GeneratingUnit and SynchronousMachine mRIDs;
- target case Unit name;
- mapping basis and explicit approval state.

The RTS profile groups candidates by exact Node and exact `Max Capacity`. A
single candidate is direct. Multiple physically equivalent units are paired by
the source unit ordinal and target PSS/E machine-ID order, then materialized in
the reviewable crosswalk. No mapping is applied until `approved=true`.

At runtime, `case.GetUnit(target_machine_name).GetRdfID()` must exactly equal the
approved target mRID before `SetGeneration()` is called.

The RTS load benchmark uses exact source bus + load ID, matching base P/Q and
target EnergyConsumer mRID. The RTS generator and load builders are benchmark
profiles only. Generic production conversion consumes externally reviewed,
approved crosswalks and does not infer identity using RTS naming rules.

## OperatingSnapshot

The v2 typed JSON boundary contains source wall-clock/time-basis/timezone and
analysis-timezone separately, plus generator setpoints, load P/Q,
unit-status actions, optional voltage/Mvar targets, audit-only units and source
hashes. Active load comes from the authoritative RTS regional forecast. Reactive
load uses the explicit `preserve_base_pf` AC embedding policy and carries derived
provenance.

Commitment is never inferred from `Generation == 0`. Thermal/hydro binary status,
variable-resource ON-only behavior and synchronous-condenser preserve behavior
are separate policies. Storage and CSP remain typed preserve resources until
their charging/discharging contracts are commissioned.

## Snapshot selection

A snapshot is uniquely selected by phase, period, source wall clock and
sample. No timezone is attached to a naive PLEXOS value. `timestamp_utc` exists
only when source time basis is explicitly known; analysis timezone is a separate
ODMS embedding choice. The native reader accepts only `ST / Interval / Generator / Generation` as native ZIP
dispatch. Ambiguous stochastic samples, duplicate timestamps, duplicate targets,
non-power units, out-of-range values and incomplete mappings fail closed.

## Time-series policy

PLEXOS remains the authoritative external time-series store. ODMS Network
Analysis receives one snapshot at a time. `run-timeseries` launches a fresh
ODMS process and calls `BuildCase` for every timestamp, then writes an aggregate
CSV and hash-bearing manifest. It does not populate the native
Season/DayType/TimeOfDay schedule schema and does not infer commitment from
`Generation == 0`.

Runtime modes are `analysis-only` and `sv-store`. `native-schedule` is reserved
and rejected; `StoreSolutionState()` persists SV and is not represented as a
native ODMS schedule.

## Transaction boundary

- Build the case and verify every identity before the first mutation.
- Apply load, status and generation layers in deterministic order.
- Refresh status objects with `Init()` and read back P/Q/status within tolerance.
- Require balanced preflight and audit `MismatchDistribution=SwingBus`.
- Solve PF.
- Use `PowerFlowSummary` for solved generation/load/loss balance; ODMS does not
  expose swing compensation through `Unit.PresentMW` for this benchmark.
- Report the difference as system-level `unattributed_swing_mw`; never assign it
  to a machine without authoritative API evidence.
- Gate configured bus voltage, generator status/limits, and branch loading when
  positive ratings are available; explicitly report unrated branches.
- Call `StoreSolutionState()` only after convergence, postflight residual gate
  and explicit user authorization.
- Close the in-memory case on success or failure.
