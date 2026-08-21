# Changelog

## 1.1.0 — final adapter freeze

- Added source-agnostic normalized load-series input and retained RTS regional
  load allocation as an explicit profile feeding the same runtime contract.
- Added strict timestamp, identity, approval, uniqueness and finite-value gates,
  plus provenance/Q-policy and load-mode audit through batch manifests.
- Added Python 3.13 CI with both test and native Solution ZIP dependencies.

## 1.0.0

- Added native Energy Exemplar Solution ZIP timestamp enumeration, cached
  Generation/Units Generating reads and 24-hour public-file build acceptance.
- Froze validate-only Q-limit behavior and real ODMS 24-hour commissioning.

## 0.4.1

- Added deterministic Q-limit readback validation without runtime mutation.

## 0.4.0

- Added independent time-series execution, resume fingerprinting, timeout and
  engineering outcome classification.

## 0.3.0

- Added branch rating contracts, base-AC calibration and expanded PF audit.

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
