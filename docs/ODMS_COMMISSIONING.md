# ODMS commissioning

The supplied worker runs inside ODMS, not ordinary CPython. ODMS 14.2.3.1 uses
Python 3.13 bindings, so `scripts/run_odms_snapshot.ps1` prepends Python 3.13 to
the child process `PATH`.

Safe commissioning order:

1. Run `build-snapshot` and require validation PASS.
2. Review the approved crosswalk and the two source hashes in the audit.
3. Run the ODMS launcher without `-StoreSV`.
4. Require exact Unit mRID resolution and ScheduledMW readback within tolerance.
5. Require PF convergence and inspect slack/loss behavior.
6. Only then rerun with `-StoreSV` if persistence is desired.

The response JSON separates requested dispatch, initialized `ScheduledMW` and
solved `PresentMW/Mvar/kV`. A slack-machine difference after PF is not by itself
an adapter error.
