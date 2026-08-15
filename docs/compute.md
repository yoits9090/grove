# Reproducible compute plan

The canonical remote evaluation path is GitHub Actions in `.github/workflows/evals.yml`.
It uses no external credentials or private datasets, pins uv, runs the full
correctness matrix on Python 3.10 through 3.14, and stores raw JUnit, environment,
benchmark, and manifest artifacts. Pull requests run correctness only; scheduled
and manually dispatched runs also execute the observational smoke benchmark.

Timing results from hosted runners are grouped by commit, workload, runner, and
Python version. They are not treated as absolute quality claims. Correctness is
the hard gate: zero invariant violations, acknowledged data loss, mixed
transactions, or unexplained flakes.

For Colab, checkout an explicit commit SHA rather than `main`, install the local
package, and call the tracked benchmark/test commands. Colab timing is not
comparable to Actions and is intended for ad-hoc reproduction or long fuzzing,
not release gating. Never place GitHub tokens, cloud credentials, or private
artifacts in a notebook.
