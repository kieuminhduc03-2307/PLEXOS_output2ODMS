# PLEXOS Output → PSS ODMS Dispatch Adapter

This repository converts one timestamp from a PLEXOS optimized solution into a
validated PSS®ODMS operating snapshot. It is deliberately separate from the
PLEXOS model XML ↔ CIM equipment adapter.

```text
PLEXOS Solution / Generation table
                  │
                  ▼
       exact timestamp selection
       phase / period / sample / unit validation
                  │
                  ▼
       approved Generator + Load crosswalks
       PLEXOS GUID / RTS bus+load ID → ODMS mRID
                  │
          ┌───────┴────────┐
          ▼                ▼
 ODMS Load.SetLoad(P,Q) → class-aware status → Unit.SetGeneration(P,Q)
          │
          ▼
       Power Flow
          │
          ▼
 PresentMW/Mvar/kV; optional StoreSolutionState() after convergence
```

The adapter never writes dispatch to `GeneratingUnit.nominalP`,
`GeneratingUnit.maxOperatingP`, SQL equipment tables, or directly to SV.

## Supported V1 inputs

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

Build and explicitly approve the reviewable crosswalk:

```powershell
python -m plexos_output2odms build-crosswalk `
  "D:\ODMS\Test Adapter\PLEXOS_output\RTS-GMLC\RTS-GMLC.txt" `
  "D:\ODMS\tmp\rts_gmlc_odms_compare\odms_cim17\odms_rts_gmlc_cim17.xml" `
  "D:\ODMS\tmp\plexos_output2odms_rts\generator_crosswalk.json" `
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

Build one timestamp:

```powershell
python -m plexos_output2odms build-snapshot `
  "D:\ODMS\Test Adapter\PLEXOS_output\RTS-GMLC\PLEXOS_DA_solution_generation.txt" `
  "D:\ODMS\tmp\plexos_output2odms_rts\generator_crosswalk.json" `
  "D:\ODMS\tmp\rts_gmlc_odms_compare\odms_cim17\odms_rts_gmlc_cim17.xml" `
  "D:\ODMS\tmp\plexos_output2odms_rts\snapshot_20200705T0000" `
  --timestamp "2020-07-05T00:00:00" `
  --timezone "Asia/Ho_Chi_Minh" `
  --unit MW `
  --missing-dispatch preserve `
  --regional-load "D:\...\Load\DAY_AHEAD_regional_Load.csv" `
  --load-crosswalk "D:\ODMS\tmp\plexos_output2odms_rts\load_crosswalk.json" `
  --commitment "D:\...\allTX\PLEXOS_DA_solution_commitment.csv"
```

Outputs:

- `dispatch.normalized.csv`: requested `ScheduledMW`, identity and CIM sign.
- `load.normalized.csv`: authoritative nodal P allocation and derived Q embedding.
- `status.normalized.csv`: class-aware commitment action or explicit preserve.
- `operating_snapshot.json`: complete typed runtime artifact.
- `dispatch.validation.json`: fail-closed findings.
- `dispatch.audit.json`: source hashes, selection and mapping totals.
- `PLEXOS_DISPATCH_SSH.xml`: CIM17 SSH representation.

`--missing-dispatch preserve` is explicit. In the current result table,
`212_CSP_1` and `313_STORAGE_1` are absent, so their existing ODMS scheduled
values are left unchanged rather than silently set to zero.

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
requires PF convergence and a system balance residual within `0.001 MW` before
`StoreSolutionState()` is allowed. `PowerFlowSummary.GenerationMW` is the
authoritative solved total because ODMS `Unit.PresentMW` does not expose the
swing-compensation component in this case.

The ODMS 14.2 installation links Python 3.13; the launcher temporarily prepends
the local Python 3.13 runtime to `PATH` for the child process.

See [RTS-GMLC acceptance](docs/RTS_GMLC_ACCEPTANCE.md) and
[architecture](docs/ARCHITECTURE.md) for the exact result and safety boundary.
