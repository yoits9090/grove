# Direct SQLite scalar-index adversarial evidence

## Scope

This report covers the disposable `SQLiteScalarPropertyIndexExperiment` direct
SQL scalar lookup path. It does not change the core `SQLiteTreeStore` API or
claim the experiment is production-ready. The focused suite is
`tests/test_sqlite_scalar_adversarial.py`.

## Results

The suite exercises:

- invalid sidecar bytes and extra sidecar tables, plus invalid identity/revision
  metadata; all fail closed with `StorageCorruptionError`;
- dangling candidate rows; a lookup for the dangling key fails closed rather
  than returning a source row that does not exist;
- stale sidecars after commits made by a separate core-store handle, including
  source node deletion and creation, and stale metadata on reopen; all rebuild
  from the source snapshot;
- scalar type tags for `None`, booleans, integers (including values larger than
  SQLite's signed 64-bit range), floats, signed zero, strings, bytes,
  timezone-aware datetimes, and `Reference`; exact typed equality keeps `True`,
  `1`, and `1.0` distinct, while equivalent datetime offsets normalize to one
  instant;
- scoped traversal by path, node object, and ID, with recursive and
  `include_root` combinations, mapping/callable predicates, and node types;
- separate-interpreter `SIGKILL` boundaries during sidecar rewrite, source
  revision update, and immediately after SQL `COMMIT`. Reopen observes the
  complete prior snapshot before commit and the complete new snapshot after
  commit.

Run from the repository root:

```text
uv run pytest -q tests/test_sqlite_scalar_adversarial.py
```

The focused run passed with 13 tests on the checked-out macOS/Python runtime.
The existing scalar experiment tests and SQLite crash tests also pass:

```text
uv run pytest -q tests/test_sqlite_property_index_experiment.py tests/test_sqlite_crash.py
```

## Known integrity boundary

Sidecar metadata stores only source identity and source revision. It is a
revision validator, not a checksum of sidecar rows. If an external process
deletes or changes sidecar rows while leaving metadata and source revision
unchanged, the experiment can return a false negative (or ignore an unrelated
row). The characterization test `test_same_revision_sidecar_tamper_is_a_known_integrity_limit`
records this behavior explicitly. A production design would need a sidecar
checksum/row count, authenticated storage, or a rebuild policy before exposing
this direct path as public API.
