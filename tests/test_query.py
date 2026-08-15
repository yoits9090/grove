import datetime as dt
from grove import PropertyIndex, Query, Reference, SQLiteTreeStore, TreeStore

def test_query_snapshot_traversal_filters_and_typed_index():
    db=TreeStore(); root=db.create("root",type="folder")
    a=db.create("a",parent=root.id,type="leaf",properties={"rank":1,"nested":{"ok":True}})
    b=db.create("b",parent=root.id,type="leaf",properties={"rank":True})
    q=db.query(root.id,include_root=True).by_type("leaf")
    assert [n.name for n in q]==["a","b"]
    assert [n.name for n in db.query(root.id).where({"rank":1})]==["a"]
    snap=db.query(root.id)
    db.rename(a.id,"renamed")
    assert [n.name for n in snap]==["a","b"]
    assert [n.name for n in db.query(root.id)]==["renamed","b"]
    idx=db.create_index("rank")
    assert idx.ids(1)==(a.id,)
    assert idx.ids(True)==(b.id,)

def test_query_recursive_scope_and_detached_values():
    db=TreeStore(); a=db.create("a"); b=db.create("b",parent=a.id,properties={"v":[1]}); c=db.create("c",parent=b.id)
    assert [n.name for n in db.query(a.id,recursive=False)]==["b"]
    assert [n.name for n in db.query(a.id,recursive=False,include_root=True)]==["a","b"]
    assert [n.name for n in db.query(a.id,include_root=True)]==["a","b","c"]
    n=db.query(a.id).where(lambda node: node.name=="b").first(); n.properties["v"].append(2)
    assert db.get(b.id).properties["v"]==[1]

def test_query_on_sqlite_refreshes_and_index_survives_reopen(tmp_path):
    p=tmp_path/"q.db"
    with SQLiteTreeStore(p) as db:
        a=db.create("a",properties={"tag":"x"}); assert db.query().where(tag="x").first().id==a.id
    with SQLiteTreeStore(p) as db:
        assert db.create_index("tag").ids("x")== (a.id,)


def test_transaction_query_reads_staged_state_only():
    db=TreeStore(); db.create("existing")
    with db.transaction() as tx:
        staged=tx.create("staged", properties={"ready": True})
        assert [node.name for node in tx.query(predicate={"ready": True})] == ["staged"]
        assert [node.name for node in tx.find(type="object")] == ["existing", "staged"]
    assert db.exists("/staged")
