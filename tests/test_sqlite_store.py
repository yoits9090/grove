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
