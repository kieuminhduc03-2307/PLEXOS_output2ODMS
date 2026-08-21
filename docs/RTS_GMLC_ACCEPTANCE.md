# RTS-GMLC acceptance - 2026-08-21

## Golden operating point

- Source wall clock: `2020-07-05 00:00` (RTS Period 1).
- Source time basis/timezone: `unknown_local` / null. UTC is not asserted.
- Analysis embedding timezone: UTC.
- PLEXOS Generation: local allTX result, 156 columns.
- Commitment: official allTX `PLEXOS_DA_solution_commitment.csv`; its Generation
  companion was compared with the local file and all 156 values were identical.
- Load P: official `DAY_AHEAD_regional_Load.csv` allocated using official
  `bus.csv` proportions.
- Load Q: derived AC embedding `preserve_base_pf`, not claimed as PLEXOS output.
- Target: ODMS 14.2.3.1 model `RTS-GMLC` on `.\SQLEXPRESS`.

## Conversion and preflight

| Check | Result |
|---|---:|
| Generator crosswalk | 158 approved |
| Generator setpoints | 156/156 mapped |
| Load crosswalk/setpoints | 51/51 approved and mapped |
| Commitment/status rows | 156 class-aware |
| PLEXOS/ODMS requested generation | 4474.979379 MW |
| RTS requested load P | 4474.979379 MW |
| Requested P imbalance | 9.09e-13 MW |
| ODMS generator readback | 4474.979378700256 MW |
| ODMS load P readback | 4474.979372024536 MW |
| ODMS readback P imbalance | 6.68e-6 MW |
| ODMS load Q readback | 910.697551727295 Mvar |
| Generator max readback error | 1.221e-5 MW |
| Readback tolerance | 1e-4 MW |

The absent `212_CSP_1` and `313_STORAGE_1` remain explicit preserve targets.
The other two ODMS-only machines (`113_DC`, `316_DC`) are audit-only targets.
All 160 ODMS SynchronousMachine identities are therefore accounted for.

## Commitment policy

- CT/CC/steam/nuclear/hydro: explicit binary `Units Generating` controls status.
- Wind/PV/RTPV: positive commitment may turn the unit on; zero preserves status.
- Synchronous condenser: always preserve; zero active commitment never trips it.
- CSP/storage: preserve pending typed operating semantics.

ODMS requires `Unit.Init()` after `SetDeviceStatus()` to refresh readback from
case memory. The worker verifies every requested state after that refresh. In
this snapshot 51 base-case statuses changed.

## Real ODMS PF acceptance

| Check | Result |
|---|---:|
| Mismatch distribution | SwingBus (enum 0, before and after) |
| PF | CONVERGED |
| System GenerationMW | 4556.037109375 MW |
| System LoadMW | 4474.9794921875 MW |
| System LossMW | 81.0573501587 MW |
| Active balance residual | 0.000267029 MW |
| Postflight tolerance | max(0.001 MW, 0.01% system scale) |
| Largest mismatch | 0.000721191 MVA |
| Total mismatch | 0.000923740 MVA |
| SV persisted | No |

The correct solved assertion is `GenerationMW ≈ LoadMW + LossMW`, not that
solved generation remains equal to lossless PLEXOS dispatch. ODMS
`Unit.PresentMW` does not expose the swing-compensation component in this case;
`PowerFlowSummary` is therefore the authoritative system-balance source. Six
priority-0 units at bus 113 identify the swing assignment group.

`StoreSolutionState()` remains disabled in the commissioning run. If requested,
the worker now requires convergence and the postflight balance gate before it
can persist SV.

## V0.3 336-hour commissioning

Every timestamp from `2020-07-05 00:00` through `2020-07-18 23:00` was run in a
fresh hidden `ODMS.exe` process with a fresh `BuildCase`. Mode was
`analysis-only`, status mode was `crosswalk_commitment`, voltage gate was
0.90–1.10 pu, and SV was not stored.

| Check | Result |
|---|---:|
| Independent snapshots built | 336/336 |
| Identity/readback mapping failures | 0 |
| PF converged | 316/336 |
| Passed all gates | 79/336 |
| Voltage-gate failures | 233 |
| Balance-gate failures after convergence | 4 |
| PF non-convergence | 20 |
| Generator-limit/status violation hours | 0 |
| Rated-branch overload hours | 0 |
| Maximum generator readback error | 3.025e-5 MW |
| Maximum absolute system residual | 1.1096 MW |
| Solved voltage range (312 gated hours) | 0.70215–1.17382 pu |
| In-service branches discovered | 120 |
| Branches with usable Condition-A rating | 0 |

All 120 in-service branch devices were returned by the ODMS API, but this
imported case exposes no positive Condition-A limits. The adapter therefore
reports 120 unrated branches rather than claiming an overload pass from absent
ratings.

The remaining AC failures are not converter identity errors. The available
PLEXOS output supplies active dispatch and commitment, but no reactive dispatch
or voltage targets. The current explicit AC embedding derives load Q with
`preserve_base_pf`; it cannot guarantee an acceptable AC voltage profile for
all DC-optimized operating points. Examples are BAKER (minimum 0.70215 pu) and
CAMUS (maximum 1.17382 pu). These snapshots remain fail-closed until an approved
Q/voltage/control policy or authoritative PLEXOS outputs are provided.

`unattributed_swing_mw` is recorded as
`PowerFlowSummary.GenerationMW - sum(Unit.PresentMW)` at system level only. It
is never assigned to a specific generator.
