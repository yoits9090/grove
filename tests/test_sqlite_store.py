import datetime as dt
import multiprocessing as mp
import sqlite3
import pytest
from grove import SQLiteTreeStore, Reference, InvalidOperationError, StorageCorruptionError

def test_sqlite_roundtrip_order_and_pragmas(tmp_path):
    path=tmp_path/"grove.db"
    db=SQLiteTreeStore(path)
    assert db._conn.execute("PRAGMA journal_mode").fetchone()[0].lower()=="wal"
    assert db._conn.execute("PRAGMA foreign_keys").fetchone()[0]==1
    assert db._conn.execute("PRAGMA synchronous").fetchone()[0]==2
    root=db.create("root",properties={"when":dt.datetime(2020,1,1,tzinfo=dt.timezone.utc),"ref":Reference("external")})
    a=db.create("a",parent=root.id); b=db.create("b",parent=root.id,index=0)
    assert db.get(root.id).children==(b.id,a.id)
    db.close()
    with SQLiteTreeStore(path) as reopened:
        assert reopened.get("/root").id == root.id
        assert reopened.path(b.id)=="/root/b"
        assert reopened.get(root.id).properties["ref"]==Reference("external")

def test_sqlite_transaction_conflict_across_instances(tmp_path):
    path=tmp_path/"conflict.db"
    one=SQLiteTreeStore(path); two=SQLiteTreeStore(path)
    t1=one.transaction(); t2=two.transaction()
    t1.create("one"); t1.commit()
    t2.create("two")
    with pytest.raises(InvalidOperationError): t2.commit()
    with SQLiteTreeStore(path) as check:
        assert check.exists("/one") and not check.exists("/two")
    one.close(); two.close()

def test_sqlite_move_delete_copy_and_reopen(tmp_path):
    path=tmp_path/"ops.db"
    with SQLiteTreeStore(path) as db:
        a=db.create("a"); b=db.create("b"); x=db.create("x",parent=a.id)
        db.move(x.id,b.id); copied=db.copy(b.id,"/",name="b2")
        assert db.path(x.id)=="/b/x" and len(db.get(copied.id).children)==1
        db.delete(a.id,recursive=True)
    with SQLiteTreeStore(path) as db:
        assert db.exists("/b/x") and db.exists("/b2/x") and not db.exists("/a")

def test_sqlite_non_mapping_properties_fail_closed(tmp_path):
    path = tmp_path / "bad-properties.db"
    with SQLiteTreeStore(path) as db:
        db.create("payload", properties={"ok": True})
    conn = sqlite3.connect(path)
    conn.execute("UPDATE nodes SET properties = 'null' WHERE name = 'payload'")
    conn.commit()
    conn.close()
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(path)


def test_sqlite_malformed_rows_fail_closed(tmp_path):
    path=tmp_path/"bad.db"
    with SQLiteTreeStore(path) as db: db.create("ok")
    conn=sqlite3.connect(path); conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("UPDATE nodes SET parent_id='missing' WHERE name='ok'"); conn.commit(); conn.close()
    with pytest.raises(StorageCorruptionError): SQLiteTreeStore(path)

def _writer(path, name):
    for attempt in range(5):
        try:
            with SQLiteTreeStore(path) as db: db.create(name)
            return
        except InvalidOperationError:
            if attempt == 4: raise

def test_sqlite_process_writers_are_serializable(tmp_path):
    path=str(tmp_path/"writers.db")
    with SQLiteTreeStore(path): pass
    procs=[mp.Process(target=_writer,args=(path,f"n{i}")) for i in range(4)]
    for p in procs:p.start()
    for p in procs:p.join(10)
    assert all(p.exitcode==0 for p in procs)
    with SQLiteTreeStore(path) as db:
        assert all(db.exists(f"/n{i}") for i in range(4))


def test_sqlite_order_mutations_survive_reopen(tmp_path):
    path=tmp_path/"order.db"
    with SQLiteTreeStore(path) as db:
        a=db.create("a"); b=db.create("b"); c=db.create("c")
        db.move(a.id,"/",index=2); db.delete(c.id); d=db.create("d",index=1)
        assert db.root.children==(b.id,d.id,a.id)
    with SQLiteTreeStore(path) as db:
        assert tuple(db.get("/").children)==(b.id,d.id,a.id)


def test_sqlite_stale_transaction_conflicts_after_other_handle_commit(tmp_path):
    path=tmp_path/"stale.db"
    with SQLiteTreeStore(path) as first, SQLiteTreeStore(path) as second:
        tx=first.transaction(); tx.create("pending")
        second.create("committed")
        assert first.exists("/committed")
        with pytest.raises(InvalidOperationError): tx.commit()
        assert not first.exists("/pending")


def test_sqlite_reads_skip_snapshot_materialization_when_revision_is_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "fast-read.db"
    with SQLiteTreeStore(path) as db:
        node = db.create("node")
        original = db._read_state_from_connection
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(db, "_read_state_from_connection", counted)
        for _ in range(20):
            assert db.get(node.id).id == node.id
        assert calls == 0


def test_sqlite_fast_read_path_still_refreshes_other_instance(tmp_path):
    path = tmp_path / "fast-read-refresh.db"
    with SQLiteTreeStore(path) as first, SQLiteTreeStore(path) as second:
        node = first.create("from-first")
        assert second.get(node.id).name == "from-first"



def test_sqlite_raw_handle_mutation_invalidates_read_cache(tmp_path):
    """The read fast path must notice writes made outside GROVE."""
    path = tmp_path / "raw-mutation.db"
    with SQLiteTreeStore(path) as db:
        node = db.create("before")
        with sqlite3.connect(path) as raw:
            raw.execute("UPDATE nodes SET name = 'after' WHERE id = ?", (node.id,))
            raw.commit()
        assert db.get(node.id).name == "after"


def test_sqlite_closed_store_rejects_public_resources(tmp_path):
    path = tmp_path / "closed.db"
    db = SQLiteTreeStore(path)
    node = db.create("node")
    tx = db.transaction()
    db.close()

    operations = (
        lambda: db.get(node.id),
        lambda: db.exists(node.id),
        lambda: db.path(node.id),
        lambda: db.export(),
        lambda: db.export_json(),
        lambda: db.query().all(),
        lambda: db.subscribe(lambda _change: None),
        lambda: db.index_property("kind"),
        lambda: db.drop_index("kind"),
        db.transaction,
    )
    for operation in operations:
        with pytest.raises(InvalidOperationError, match="store is closed"):
            operation()

    # A transaction snapshot is detached and remains readable, but publishing
    # it after its owning durable resource closes must fail closed.
    assert tx.get(node.id).id == node.id
    with pytest.raises(InvalidOperationError, match="store is closed"):
        tx.commit()


def test_sqlite_schema_rejects_a_second_root(tmp_path):
    path = tmp_path / "second-root.db"
    with SQLiteTreeStore(path) as db:
        parent = db.create("parent")
        child = db.create("child", parent=parent.id)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        # Keep the row otherwise valid but detach it from the primary root.
        conn.execute("UPDATE nodes SET parent_id = NULL WHERE id = ?", (child.id,))
        conn.execute("DELETE FROM children WHERE child_id = ?", (child.id,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(path)



def _write_relaxed_sqlite_schema(path, *, duplicate_metadata=False, duplicate_edge=False):
    """Build rows without SQLite constraints to exercise defensive readers."""
    now = "2020-01-01T00:00:00+00:00"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE metadata (id INTEGER, revision INTEGER, root_id TEXT);
        CREATE TABLE nodes (
            id TEXT, name TEXT, type TEXT, properties TEXT, parent_id TEXT,
            created_at TEXT, modified_at TEXT
        );
        CREATE TABLE children (parent_id TEXT, child_id TEXT, position INTEGER);
        """
    )
    conn.execute("INSERT INTO metadata VALUES (1, 0, 'root')")
    if duplicate_metadata:
        conn.execute("INSERT INTO metadata VALUES (2, 0, 'root')")
    conn.executemany(
        "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("root", "", "root", "{}", None, now, now),
            ("child", "child", "object", "{}", "root", now, now),
        ],
    )
    conn.execute("INSERT INTO children VALUES ('root', 'child', 0)")
    if duplicate_edge:
        conn.execute("INSERT INTO children VALUES ('root', 'child', 1)")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    "kwargs",
    ({"duplicate_metadata": True}, {"duplicate_edge": True}),
    ids=("duplicate-metadata", "duplicate-child-edge"),
)
def test_sqlite_relational_duplicates_fail_closed(tmp_path, kwargs):
    path = tmp_path / "duplicate-relational-row.db"
    _write_relaxed_sqlite_schema(path, **kwargs)
    with pytest.raises(StorageCorruptionError):
        SQLiteTreeStore(path)


def test_sqlite_out_of_band_schema_drop_fails_closed_on_existing_handle(tmp_path):
    path=tmp_path/"dropped.db"
    db=SQLiteTreeStore(path); node=db.create("node")
    with sqlite3.connect(path) as raw:
        raw.execute("DROP TABLE children")
        raw.commit()
    with pytest.raises(StorageCorruptionError): db.get(node.id)
    db.close()



def test_sqlite_online_backup_is_consistent_and_detached(tmp_path):
    source_path = tmp_path / "live.db"
    backup_path = tmp_path / "backup.db"
    with SQLiteTreeStore(source_path) as source:
        source.create("before")
        source.backup(backup_path)
        source.create("after")
        # Replacing the destination is supported and should remain atomic.
        source.backup(backup_path)
    with SQLiteTreeStore(backup_path) as backup:
        assert backup.exists("/before")
        assert backup.exists("/after")


def test_sqlite_backup_rejects_closed_or_aliased_resources(tmp_path):
    path = tmp_path / "source.db"
    destination = tmp_path / "copy.db"
    db = SQLiteTreeStore(path)
    db.create("node")
    with pytest.raises(InvalidOperationError, match="source"):
        db.backup(path)
    active = sqlite3.connect(":memory:")
    active.execute("BEGIN")
    try:
        db = SQLiteTreeStore(path)
        with pytest.raises(InvalidOperationError, match="active transaction"):
            db.backup(active)
        db.close()
    finally:
        active.rollback()
        active.close()
    db = SQLiteTreeStore(path)
    db.close()
    with pytest.raises(InvalidOperationError, match="store is closed"):
        db.backup(destination)


def test_sqlite_writer_lock_failure_is_bounded(tmp_path):
    path = tmp_path / "busy.db"
    with SQLiteTreeStore(path):
        pass
    holder = sqlite3.connect(path, timeout=0, isolation_level=None)
    db = None
    try:
        holder.execute("BEGIN IMMEDIATE")
        # Initialization itself is expected to fail boundedly while another
        # process owns the writer lock.
        with pytest.raises(InvalidOperationError, match="writer lock unavailable"):
            SQLiteTreeStore(path, timeout=0, write_retries=1, retry_delay=0)
        holder.rollback()
        db = SQLiteTreeStore(path, timeout=0, write_retries=1, retry_delay=0)
        holder.execute("BEGIN IMMEDIATE")
        with pytest.raises(InvalidOperationError, match="writer lock unavailable"):
            db.create("blocked")
        assert not db.exists("/blocked")
    finally:
        if db is not None:
            db.close()
        holder.rollback()
        holder.close()
