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

## V0.4 base AC calibration

The real ODMS base case was audited read-only, then compared with official RTS
`gen.csv`, `bus.csv`, and `branch.csv` before any time-series run.

| Check | Result |
|---|---:|
| ODMS buses / loads / units / branches | 73 / 51 / 160 / 120 |
| Official generators mapped | 158/158 |
| ODMS regulating / non-regulating mapped units | 97 / 61 |
| Qmin/Qmax mismatches | 0 |
| Maximum base ScheduledMW error | 1.221e-6 MW |
| Maximum base ScheduledMvar reference error | 4.883e-6 Mvar |
| Maximum regulating voltage-target error | 0.004583 kV |
| Official loads mapped, P/Q mismatches | 51/51, 0 |
| Official branches mapped | 120/120 |
| Transformer controls | 15 fixed-voltage, tap changing disabled |
| Fixed shunt banks / SVCs | 3 / 0 |

`MVAR Inj` is used only to prove the ODMS base-case calibration; it is not Q(t).
PLEXOS remains authoritative for P(t) and commitment, while ODMS solves Q.
Voltage targets come from generator `V Setpoint p.u.` and are applied only to
the 97 ODMS regulating units. Transformer and shunt states are audited and
preserved rather than automatically manipulated.

The largest solved-voltage difference from `bus.csv` is 0.087954 pu. This is an
input-solution distinction, not an identity mismatch: at buses such as 207 and
307, generator setpoints (0.9699 and 0.9568 pu) differ materially from the
voltages recorded in `bus.csv`; the ODMS controls agree with `gen.csv`.

All 120 official branches map exactly by terminal buses and electrical
parameters. The reviewed rating contract is `Cont → ConditionA`,
`LTE → ConditionB`, `STE → ConditionC`. The same-case RAW file verifies the
continuous rating but stores RATEA/B/C identically; reviewed `branch.csv` is
therefore authoritative for distinct emergency ratings.

## V0.4 commissioning

Every timestamp from `2020-07-05 00:00` through `2020-07-18 23:00` was run in a
fresh hidden `ODMS.exe` process with a fresh `BuildCase`. Mode was
`analysis-only`, status mode was `crosswalk_commitment`, voltage gate was
0.90–1.10 pu, and SV was not stored.

| Check | Result |
|---|---:|
| Independent snapshots built | 336/336 |
| Identity/readback mapping failures | 0 |
| PF converged | 316/336 |
| Adapter-valid snapshots | 336/336 |
| AC-valid snapshots | 44/336 |
| Voltage-gate failures | 233 |
| Balance-gate failures after convergence | 4 |
| PF non-convergence | 20 |
| Generator-limit/status violation hours | 0 |
| Rated-branch overload hours | 167 |
| Maximum generator readback error | 3.025e-5 MW |
| Maximum absolute residual in the four failed balance gates | 1.1096 MW |
| Solved voltage range (312 gated hours) | 0.70215–1.17382 pu |
| Maximum branch loading | 111.337% |
| Rated branches after readback | 120 |
| Missing mapping/control/limit data | 0 |

Primary outcome classes are 44 `ADAPTER_VALID_AC_VALID`, 167
`ADAPTER_VALID_AC_OVERLOAD`, 101 `ADAPTER_VALID_AC_VOLTAGE_VIOLATION`, 20
`ADAPTER_VALID_AC_NONCONVERGED`, and 4
`ADAPTER_VALID_ACCOUNTING_RESIDUAL`. Because voltage and overload can occur
together, independent flags report 233 voltage-violation hours and 167 overload
hours. No tolerance was widened to force a pass.

The four accounting residuals occur at `2020-07-09 10:00` (-0.817413 MW),
`2020-07-09 14:00` (-0.873947 MW), `2020-07-10 11:00` (-1.109512 MW), and
`2020-07-16 10:00` (-1.074280 MW). They are now a separate deterministic
classification rather than being conflated with mapping or PF convergence.

The remaining AC failures are not converter identity errors. The available
PLEXOS output supplies active dispatch and commitment but no reactive dispatch.
The authoritative static AC contract is present and load Q uses
`preserve_base_pf`; these inputs still cannot guarantee an acceptable AC state
for every optimized active-power schedule. Examples are BAKER (minimum 0.70215
pu) and CAMUS (maximum 1.17382 pu). The adapter records these outcomes without
turning on committed-off thermal units or changing preserved transformer/shunt
controls merely to obtain convergence.

`unattributed_swing_mw` is recorded as
`PowerFlowSummary.GenerationMW - sum(Unit.PresentMW)` at system level only. It
is never assigned to a specific generator.
