# Changelog

## 0.1.0

- Add wide result, Energy Exemplar query CSV and native Solution ZIP readers.
- Enforce ST/Interval/timestamp/timezone/sample/unit selection.
- Add approval-gated RTS-GMLC Generator-to-ODMS crosswalk.
- Generate normalized dispatch, validation, audit and deterministic CIM17 SSH.
- Apply dispatch to real ODMS `Unit.ScheduledMW`, verify runtime mRID/readback,
  solve PF and gate SV persistence on convergence plus explicit authorization.
- Validate the 2020-07-05 00:00 RTS-GMLC snapshot: 156/156 dispatch values and
  SSH values exact; real ODMS initialization 156/156 within 0.0001 MW.
