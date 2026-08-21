# Changelog

## 0.2.0

- Added approved RTS bus/load-ID → ODMS mRID crosswalks.
- Added official RTS regional-to-nodal active load allocation and auditable
  `preserve_base_pf` reactive embedding.
- Added typed `OperatingSnapshot` JSON plus normalized load/status artifacts.
- Added class-aware PLEXOS `Units Generating` handling: binary thermal/hydro,
  ON-only variable renewables, preserved synchronous condensers.
- Added load/status/generator identity verification and deterministic runtime
  order before PF.
- Added preflight balance, SwingBus policy audit, full ODMS power-flow summary,
  AC-loss postflight gate, and StoreSV protection.
- Commissioned the 2020-07-05 00:00 RTS-GMLC snapshot successfully in ODMS
  14.2.3.1 without persisting SV.

## 0.1.0

- Add wide result, Energy Exemplar query CSV and native Solution ZIP readers.
- Enforce ST/Interval/timestamp/timezone/sample/unit selection.
- Add approval-gated RTS-GMLC Generator-to-ODMS crosswalk.
- Generate normalized dispatch, validation, audit and deterministic CIM17 SSH.
- Apply dispatch to real ODMS `Unit.ScheduledMW`, verify runtime mRID/readback,
  solve PF and gate SV persistence on convergence plus explicit authorization.
- Validate the 2020-07-05 00:00 RTS-GMLC snapshot: 156/156 dispatch values and
  SSH values exact; real ODMS initialization 156/156 within 0.0001 MW.
