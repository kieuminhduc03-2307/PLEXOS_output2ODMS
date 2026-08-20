# Architecture and semantic boundary

## Two operational representations

The authoritative execution path is:

```text
PLEXOS Generator.Generation(t) → pssoPy.Unit.ScheduledMW → PF → PresentMW/SV
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

## Snapshot selection

A snapshot is uniquely selected by phase, period, timestamp, timezone and
sample. V1 accepts only `ST / Interval / Generator / Generation` as native ZIP
dispatch. Ambiguous stochastic samples, duplicate timestamps, duplicate targets,
non-power units, out-of-range values and incomplete mappings fail closed.

## Time-series policy

PLEXOS remains the authoritative external time-series store. ODMS Network
Analysis receives one snapshot at a time. V1 does not populate the native
Season/DayType/TimeOfDay schedule schema and does not infer commitment from
`Generation == 0`.

## Transaction boundary

- Build and initialize the case in memory.
- Read back all `ScheduledMW` values within a configured tolerance.
- Solve PF.
- Collect requested, initialized and solved values separately.
- Call `StoreSolutionState()` only after convergence and only with explicit user
  authorization.
- Close the in-memory case on success or failure.
