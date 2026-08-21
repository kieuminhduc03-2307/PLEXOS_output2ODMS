# PLEXOS Output → PSS ODMS Dispatch Adapter

This repository converts one or many timestamps from a PLEXOS optimized solution into
validated PSS®ODMS operating snapshots. It is deliberately separate from the
PLEXOS model XML ↔ CIM equipment adapter.

```text
PLEXOS Solution / Generation table
                  │
                  ▼
       exact timestamp selection
       phase / period / sample / unit validation
                  │
                  ▼
       approved Generator + Load + Branch crosswalks
       PLEXOS GUID / RTS bus+load ID → ODMS mRID
                  │
          ┌───────┴────────┐
          ▼                ▼
 ODMS Load.SetLoad(P,Q) → class-aware status → Unit.SetGeneration(P,Q)
          │                         │
          │               static Q limits / regulating V setpoint
          └────────────── branch Condition A/B/C ratings
          │
          ▼
       Power Flow
          │
          ▼
 PresentMW/Mvar/kV; optional StoreSolutionState() after convergence
```

The adapter never writes dispatch to `GeneratingUnit.nominalP`,
`GeneratingUnit.maxOperatingP`, SQL equipment tables, or directly to SV.

## Supported inputs

- Native PLEXOS Solution ZIP table `ST__Interval__Generators__Generation`
  through the optional `plexosdb` dependency.
- Wide interval table used by the RTS-GMLC test:
  `time,Generator A,Generator B,...`.
- Energy Exemplar long query CSV with `child_name`, `property_name`, `_date`,
  `value` and `unit_name`.

Wide tables have no embedded unit metadata, so `--unit MW` is mandatory.
MWh/GWh summary values are rejected as dispatch.

RTS commissioning uses the upstream
[GridMod/RTS-GMLC](https://github.com/GridMod/RTS-GMLC) `bus.csv`, regional
day-ahead load and allTX commitment files. Those external datasets are not
vendored by this repository; their local paths and SHA-256 hashes are recorded
in every snapshot audit.

## Install

```powershell
python -m pip install -e ".[test,solution-zip]"
python -m pytest -q
```

## RTS-GMLC workflow

The RTS crosswalk builders are benchmark profiles, not a generic identity
algorithm. Production runs consume reviewed crosswalks whose final contracts
are PLEXOS GUID → ODMS mRID and source bus/load-ID → ODMS mRID.

Build and explicitly approve the reviewable generator crosswalk. Official
`gen.csv` supplies the static AC contract; `MW Inj`/`MVAR Inj` are base-case
calibration references, while PLEXOS Generation remains authoritative for P(t):

```powershell
python -m plexos_output2odms build-crosswalk `
  "D:\ODMS\Test Adapter\PLEXOS_output\RTS-GMLC\RTS-GMLC.txt" `
  "D:\ODMS\tmp\rts_gmlc_odms_compare\odms_cim17\odms_rts_gmlc_cim17.xml" `
  "D:\ODMS\tmp\plexos_output2odms_rts\generator_crosswalk.json" `
  --generator-data "D:\...\RTS_Data\SourceData\gen.csv" `
  --approve
```

Build the load crosswalk from official RTS `bus.csv`:

```powershell
python -m plexos_output2odms build-load-crosswalk `
  "D:\...\RTS_Data\SourceData\bus.csv" `
  "D:\ODMS\tmp\rts_gmlc_odms_compare\odms_cim17\odms_rts_gmlc_cim17.xml" `
  "D:\ODMS\tmp\plexos_output2odms_rts\load_crosswalk.json" `
  --approve
```

Audit the real ODMS base case, calibrate it against official RTS source data,
and build the branch-rating crosswalk:

```powershell
scripts\run_odms_ac_audit.ps1 `
  -ResponseJson "D:\...\base_odms_ac_audit.json" `
  -Server ".\SQLEXPRESS" -Model "RTS-GMLC"

python -m plexos_output2odms calibrate-base-ac `
  "D:\...\gen.csv" "D:\...\bus.csv" `
  "D:\...\generator_crosswalk.json" "D:\...\load_crosswalk.json" `
  "D:\...\branch_crosswalk.json" "D:\...\base_odms_ac_audit.json" `
  "D:\...\base_ac_calibration.json"

python -m plexos_output2odms build-branch-crosswalk `
  "D:\...\branch.csv" "D:\...\base_odms_ac_audit.json" `
  "D:\...\branch_crosswalk.json" --approve
```

For RTS-GMLC, the explicit thermal contract is `Cont → ConditionA`,
`LTE → ConditionB`, and `STE → ConditionC`. The same-case RAW file confirms
Condition A but collapses RATEB/RATEC to RATEA, so reviewed `branch.csv` is the
authoritative source for emergency ratings.

Build one timestamp:

```powershell
python -m plexos_output2odms build-snapshot `
  "D:\ODMS\Test Adapter\PLEXOS_output\RTS-GMLC\PLEXOS_DA_solution_generation.txt" `
  "D:\ODMS\tmp\plexos_output2odms_rts\generator_crosswalk.json" `
  "D:\ODMS\tmp\rts_gmlc_odms_compare\odms_cim17\odms_rts_gmlc_cim17.xml" `
  "D:\ODMS\tmp\plexos_output2odms_rts\snapshot_20200705T0000" `
  --timestamp "2020-07-05T00:00:00" `
  --source-time-basis unknown_local `
  --analysis-timezone UTC `
  --unit MW `
  --missing-dispatch preserve `
  --q-limit-policy validate_only `
  --regional-load "D:\...\Load\DAY_AHEAD_regional_Load.csv" `
  --load-crosswalk "D:\ODMS\tmp\plexos_output2odms_rts\load_crosswalk.json" `
  --branch-crosswalk "D:\ODMS\tmp\plexos_output2odms_rts\branch_crosswalk.json" `
  --commitment "D:\...\allTX\PLEXOS_DA_solution_commitment.csv"
```

Outputs:

- `dispatch.normalized.csv`: requested `ScheduledMW`, identity and CIM sign.
- `load.normalized.csv`: authoritative nodal P allocation and derived Q embedding.
- `status.normalized.csv`: class-aware commitment action or explicit preserve.
- `operating_snapshot.json`: complete typed runtime artifact, including static
  generator AC controls and branch ratings.
- `dispatch.validation.json`: fail-closed findings.
- `dispatch.audit.json`: source hashes, selection and mapping totals.
- `PLEXOS_DISPATCH_SSH.xml`: CIM17 SSH representation.

`--missing-dispatch preserve` is explicit. In the current result table,
`212_CSP_1` and `313_STORAGE_1` are absent, so their existing ODMS scheduled
values are left unchanged rather than silently set to zero.

PLEXOS timestamps in the RTS text exports are timezone-naive. The adapter keeps
`source_wall_clock`, `source_time_basis`, `source_timezone`, and
`analysis_timezone` separate. With `unknown_local`, `timestamp_utc` remains
null; an analysis timezone is only an ODMS embedding choice and is not claimed
as source metadata.

Run a 24-hour independent commissioning window:

```powershell
python -m plexos_output2odms run-timeseries `
  "D:\...\PLEXOS_DA_solution_generation.txt" `
  "D:\...\generator_crosswalk.json" `
  "D:\...\odms_rts_gmlc_cim17.xml" `
  "D:\...\DAY_AHEAD_regional_Load.csv" `
  "D:\...\load_crosswalk.json" `
  "D:\...\PLEXOS_DA_solution_commitment_allTX.csv" `
  "D:\...\commissioning_24h" `
  --start 2020-07-05T00:00:00 --hours 24 --unit MW `
  --branch-crosswalk "D:\...\branch_crosswalk.json" `
  --analysis-timezone UTC --mode analysis-only `
  --q-limit-policy validate_only `
  --failure-policy continue-on-error `
  --snapshot-timeout-seconds 120 --max-retries 1
```

Add `--status-mode crosswalk_commitment` to apply the reviewed commitment
policies. `dispatch_on_only` turns on positive-dispatch units and preserves zero
units; `preserve_odms` leaves every status unchanged. All modes are explicit in
the snapshot audit.

Every timestamp launches a fresh `ODMS.exe` process and calls `BuildCase`, so
state cannot leak between hours. Outputs include `timeseries_result.csv` and
atomic `run_manifest.json`. `--failure-policy` selects `fail-fast` or
`continue-on-error`; each ODMS process has a hard timeout and optional bounded
retries. `--resume` verifies a hash-bearing run fingerprint, skips completed
timestamps, and retries failed/interrupted timestamps. Reusing a populated
output directory without `--resume` is rejected. Modes are `analysis-only` and `sv-store`;
`native-schedule` is explicitly rejected until a real ODMS schedule API is
implemented. `StoreSolutionState()` is SV persistence, not a native schedule.

## Run inside ODMS

The direct integration sets `pssoPy.Unit.ScheduledMW`, reads it back, solves PF,
and stores SV only when `-StoreSV` is explicitly supplied and PF converges:

```powershell
scripts\run_odms_snapshot.ps1 `
  -OperatingSnapshot "D:\...\operating_snapshot.json" `
  -ResponseJson "D:\...\odms_pf_result.json" `
  -Server ".\SQLEXPRESS" `
  -Model "RTS-GMLC"
```

The launcher enforces/audits `MismatchDistribution=SwingBus` by default. It
requires PF convergence and a system balance residual within the greater of
`0.001 MW` or `0.01%` of system scale (to accommodate ODMS float32 summaries) before
`StoreSolutionState()` is allowed. Configurable engineering gates check bus
voltage, generator status/operating limits, and rated branch/transformer loading.
`PowerFlowSummary.GenerationMW` is the
authoritative solved total because ODMS `Unit.PresentMW` does not expose the
swing-compensation component in this case. The adapter reports the system-level
difference as `unattributed_swing_mw` and never assigns it to an individual unit.

Static Q limits are immutable by default: `validate_only` compares source
Qmin/Qmax with the ODMS case and fails on drift. `Unit.SetReactiveLimits()` is
used only when `--q-limit-policy apply_source` is explicitly selected and that
mutation is recorded in the snapshot audit.

Runtime results separate adapter validity from AC operating quality. A snapshot
can be `adapter_valid=true` while its PF is non-converged or violates voltage,
generator, or branch limits. `outcome_class` gives the primary result and
`outcome_flags` preserves simultaneous violations. Missing mapping, control, or
limit data fails closed and is never disguised by widening tolerances.

The ODMS 14.2 installation links Python 3.13; the launcher temporarily prepends
the local Python 3.13 runtime to `PATH` for the child process.

See [RTS-GMLC acceptance](docs/RTS_GMLC_ACCEPTANCE.md) and
[architecture](docs/ARCHITECTURE.md) for the exact result and safety boundary.

This repository is an RTS-GMLC reference adapter plus a reusable ODMS execution
core. Its RTS crosswalk builders, regional load allocation, and source-file
parsers are benchmark-specific. A generic production integration must provide
approved normalized identity, load, AC-control, and branch-limit contracts
instead of applying RTS naming or allocation assumptions to another model.
