# ODMS commissioning

The supplied worker runs inside ODMS, not ordinary CPython. ODMS 14.2.3.1 uses
Python 3.13 bindings, so `scripts/run_odms_snapshot.ps1` prepends Python 3.13 to
the child process `PATH`.

Safe commissioning order:

1. Run `build-snapshot` and require validation PASS.
2. Review both approved crosswalks, commitment policy and all source hashes.
3. Run the ODMS launcher without `-StoreSV`.
4. Require exact Load/Unit mRID resolution and P/Q/status readback.
5. Require preflight P balance and `MismatchDistribution=SwingBus` audit.
6. Require PF convergence and `GenerationMW ≈ LoadMW + LossMW` within the
   configured postflight tolerance.
7. Only then rerun with `-StoreSV` if persistence is desired.

The response JSON separates requested dispatch, initialized `ScheduledMW` and
solved values. For RTS-GMLC, use `PowerFlowSummary` for the system balance because
`Unit.PresentMW` excludes ODMS swing compensation. The launcher defaults to a
`0.001 MW` postflight residual gate and refuses StoreSV when it fails.
