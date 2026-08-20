# RTS-GMLC acceptance - 2026-08-21

## Inputs

- PLEXOS model: `RTS-GMLC.txt` - 158 Generator objects.
- PLEXOS dispatch: `PLEXOS_DA_solution_generation.txt` - 336 hourly rows,
  2020-07-05 00:00 through 2020-07-18 23:00, 156 Generator columns.
- ODMS 14.2.3.1 model `RTS-GMLC` on `.\SQLEXPRESS`.
- ODMS CIM17 reference - 160 SynchronousMachine resources.
- Selected timestamp: 2020-07-05 00:00 Asia/Ho_Chi_Minh.

## Conversion result

| Check | Result |
|---|---:|
| Crosswalk records | 158 |
| Dispatch columns at timestamp | 156 |
| Source values mapped to ScheduledMW | 156/156 exact in Python representation |
| SSH machine resources | 156 |
| ScheduledMW ↔ SSH `-p` | 156/156 exact |
| Duplicate source/target identities | 0 |
| Validation errors | 0 |
| PLEXOS dispatch total | 4474.979379 MW |
| Deterministic SSH SHA-256 | `317C22BFC5A51EF501E9943C63E6CD6D94BC432F4E5B2FF9D04129F04D3DA05B` |

The result table does not contain `212_CSP_1` or `313_STORAGE_1`. The acceptance
run used explicit `preserve` policy; it did not invent zero MW for them.

## Real ODMS initialization

The internal ODMS worker built the real `RTS-GMLC` case, resolved all 156 Unit
names, verified all 156 runtime RDF IDs against the approved crosswalk and called
`SetGeneration()`.

| Check | Result |
|---|---:|
| Initialized units | 156/156 |
| Requested total ScheduledMW | 4474.979379000001 MW |
| ODMS readback total | 4474.979378700256 MW |
| Maximum per-unit readback error | 0.000012207031261 MW |
| Acceptance tolerance | 0.0001 MW |
| SV persisted | No |

The small readback difference is ODMS/PSSO numeric storage precision, not an
identity or conversion error.

## PF result and remaining input gap

PF returned `Engine runtime error: 25` and did not converge. The adapter therefore
did not call `StoreSolutionState()`.

This is an operating-snapshot completeness issue: the supplied files provide
Generation but not the matching load snapshot or `Units Generating` commitment.
The ODMS case retains base-case load and status, so its operating balance is not
the PLEXOS 2020-07-05 00:00 balance. V1 deliberately does not infer commitment
from nonzero/zero generation and does not scale loads.

Acceptance for a solved snapshot requires the matching PLEXOS interval outputs:

1. Generator `Generation`;
2. Generator `Units Generating` or an approved status policy;
3. load active/reactive snapshot, or an approved load forecast mapping;
4. optional generator Mvar and voltage targets when they are authoritative.
