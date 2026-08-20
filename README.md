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
       approved Generator crosswalk
       PLEXOS GUID → ODMS SynchronousMachine mRID
                  │
          ┌───────┴────────┐
          ▼                ▼
 ODMS Unit.ScheduledMW   CIM17 SSH RotatingMachine.p
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

## Install

```powershell
python -m pip install -e ".[test,solution-zip]"
python -m pytest -q
```

## RTS-GMLC workflow

Build and explicitly approve the reviewable crosswalk:

```powershell
python -m plexos_output2odms build-crosswalk `
  "D:\ODMS\Test Adapter\PLEXOS_output\RTS-GMLC\RTS-GMLC.txt" `
  "D:\ODMS\tmp\rts_gmlc_odms_compare\odms_cim17\odms_rts_gmlc_cim17.xml" `
  "D:\ODMS\tmp\plexos_output2odms_rts\generator_crosswalk.json" `
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
  --missing-dispatch preserve
```

Outputs:

- `dispatch.normalized.csv`: requested `ScheduledMW`, identity and CIM sign.
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
  -NormalizedCsv "D:\...\dispatch.normalized.csv" `
  -ResponseJson "D:\...\odms_pf_result.json" `
  -Server ".\SQLEXPRESS" `
  -Model "RTS-GMLC"
```

The ODMS 14.2 installation links Python 3.13; the launcher temporarily prepends
the local Python 3.13 runtime to `PATH` for the child process.

See [RTS-GMLC acceptance](docs/RTS_GMLC_ACCEPTANCE.md) and
[architecture](docs/ARCHITECTURE.md) for the exact result and safety boundary.
