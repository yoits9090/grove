# Schema persistence and versioning requirements (isolated design report)

## Scope and status

This note defines the compatibility boundary for a future durable schema
catalog.  It is a design/report artifact, not a change to the public `Schema`
API or to either storage format.  The tests in
`tests/test_schema_persistence_limits.py` intentionally characterize the
current boundary so that a future implementation can change it deliberately.

GROVE schemas are currently **process-local validation configuration**.  A
`Schema` is accepted by `TreeStore`, `PersistentTreeStore`, and
`SQLiteTreeStore`, and is applied to create/update/import and to existing state
when a store is constructed.  It is not written to a persistent database or a
subtree export.  Consequently, reopening a durable store without `schema=`
does not enforce the schema that was used by its previous process.  This is an
explicit compatibility limit, not evidence that the data was schema-free.

## What is durable today

| Artifact | Current version marker | Schema information | Reopen behavior |
| --- | --- | --- | --- |
| Persistent snapshot log | JSON payload `format: 1` in `GROV1` frames | none | `PersistentTreeStore(path)` validates tree/property encoding, but has no schema to apply |
| SQLite adapter | `metadata.revision` and `metadata.root_id`; relational table shape | none (no schema catalog or schema revision) | `SQLiteTreeStore(path)` validates rows/invariants; `schema=` validates them after load |
| JSON subtree export | node records and tagged property values; no envelope version | none | import uses the receiving store's currently configured schema |
| SQLite history artifact | copy of the live SQLite database, named by logical revision | none beyond the live database | `Snapshot.open()` returns a detached store; no schema is recovered |

The storage format version and a schema version must remain separate.  A
storage format migration can change framing/tables without changing node
constraints; a schema migration can change constraints/properties while
retaining the same storage format.

A schema is not persisted by `set_schema`: currently that operation changes only
process-local validation.  It must therefore not be described as durable,
transactional metadata.  Legacy files and exports remain valid without any
schema marker, and an empty/no-op schema must not be confused with an explicitly
persisted schema.

## Required future envelope (proposal, not implemented)

If schema persistence is introduced, use a canonical, JSON-safe descriptor
rather than serializing Python objects.  A descriptor should contain at least:

```json
{
  "schema_format": 1,
  "schema_id": "application.example",
  "schema_version": 3,
  "allow_unknown_types": false,
  "types": {
    "person": {
      "properties": {
        "name": {"type": "string", "required": true},
        "age": {"type": "integer"}
      },
      "required": ["name"],
      "allow_extra": false
    }
  }
}
```

`schema_format` describes descriptor encoding and has independent compatibility
rules from the store's `format`.  `schema_id` identifies the application
contract, while `schema_version` is a non-negative, monotonically increasing
contract version.  Canonical encoding must sort map keys and set-like lists
(such as `required`), use the existing typed-value envelope for supported enum
values, and reject non-finite numbers and ambiguous values.  A canonical digest
of the descriptor may be useful for diagnostics, but must not replace the
human-controlled ID/version pair.

The current Python schema language is broader than this envelope:

* Python classes, `typing` unions, tuples, and string aliases need stable
  primitive/type names or an application registry.
* `validator`/`validate` callables (including lambdas, closures, partials, and
  local functions) cannot be safely or portably serialized.  A descriptor must
  contain a registered symbolic validator ID, not a pickle or code object.
* Enum values must use GROVE's supported property codec.  Arbitrary Python
  objects, unordered containers, and callable enum values are unsupported.
* Importing a module from a string is executable code and is not a safe default;
  the application should provide an allow-listed registry when opening a
  descriptor.

A reader that cannot represent or resolve a descriptor must fail closed with a
clear compatibility error.  It must not silently open with `Schema()` or
`allow_unknown_types=True`, because doing so removes validation at exactly the
reopen boundary where operators expect protection.

## Migration protocol requirements

1. **Explicit direction and registry.** Register migrations by
   `(schema_id, from_version, to_version)`.  Do not infer a migration from
   declaration diffs.  A downgrade is rejected unless an explicit, tested
   reverse migration exists.
2. **Validate both sides.** Load the old state and validate it with the old
   descriptor, apply a deterministic state/property/type transform, then
   validate every resulting node with the target descriptor.  A migration that
   only changes constraints (and does not transform data) is still required to
   prove target validation.
3. **One durable boundary.** For the snapshot log, append one complete frame
   containing the migrated state and new schema metadata, fsync, then publish.
   For SQLite, update schema metadata and all transformed rows in one SQL
   transaction.  A crash must reopen either the old `(state, schema_version)`
   pair or the new pair, never one without the other.
4. **No partial in-place mutation.** Failed migration, failed target
   validation, missing registry entries, and unsupported descriptor versions
   leave both data and schema metadata untouched.  A temporary copy or staged
   state is preferred for large migrations.
5. **Reopen and history semantics.** A durable handle must pin the schema
   descriptor/version it was opened with.  A history artifact must carry the
   same descriptor as its state; opening a newer/unknown descriptor is not
   allowed merely because the node rows happen to parse.
6. **Compatibility policy is explicit.** Document whether a reader supports
   exact, older, or newer schema versions.  Compatibility of storage format
   does not imply compatibility of schema contracts.  Readers may offer an
   explicitly opt-in read-only/opaque mode later, but normal CRUD reopen should
   be strict.

## Legacy and reopen matrix

| Existing artifact | Open without schema | Open with matching schema | Open with incompatible schema |
| --- | --- | --- | --- |
| Current GROVE log/SQLite file (no descriptor) | Preserve current behavior; no schema enforcement | Validate all loaded nodes before exposing store | Fail before exposing store; file remains reopenable without schema |
| Future file with descriptor known to reader | Enforce descriptor/version | Enforce and compare ID/version | Require an explicit migration, otherwise fail closed |
| Future file with unknown descriptor/version | Never silently treat as schema-free | Fail with unsupported-schema error | Fail with unsupported-schema error |
| Export from current format | Import under receiver schema | Same | Receiver validation rejects atomically |

This matrix intentionally preserves existing files while making the future
behavior unambiguous.  Introducing a catalog should add an additive marker (or
an explicitly bumped storage format), not reinterpret `format: 1` snapshots or
legacy SQLite rows as having a default schema.

## Test obligations before implementation

A schema persistence implementation should add tests for all of the following:

* canonical descriptor round-trip and deterministic bytes (including required
  ordering, typed enum values, and unsupported callables);
* old schema-free log/SQLite files reopening unchanged;
* matching descriptor enforcement on create, update, import, and reopen;
* missing/unknown descriptor and registry entries failing closed;
* explicit compatible and incompatible migrations, including type/property
  transforms and validation failures;
* crash at each migration write point reopening either complete old or complete
  new state/schema pairs;
* history artifacts retaining the schema version they were captured from; and
* failed migrations preserving the original bytes/revision and schema metadata.

Until those obligations are implemented, callers that need durable schema
semantics should keep a schema registry/version next to the database, pass the
schema explicitly on every reopen, and run `set_schema` (which validates the
complete current state) before accepting writes.  This workaround is process
coordination, not persistence, and should not be represented as a durable
migration guarantee.
