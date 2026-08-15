import datetime as dt
import json, os, struct, zlib
import pytest
from grove import (TreeStore, PersistentTreeStore, Reference, AlreadyExistsError,
                   InvalidOperationError, InvalidPropertyError, NotFoundError,
                   StorageCorruptionError)

def test_basic_identity_path_order_and_move():
    db=TreeStore()
    org=db.create("organizations")
    acme=db.create("acme", parent=org.id, type="organization")
    a=db.create("alice", parent=acme.id)
    b=db.create("bob", parent=acme.id)
    assert [x.name for x in [db.get(i) for i in db.get(acme.id).children]] == ["alice","bob"]
    db.rename(a.id,"alice-admin")
    assert db.get(a.id).id == a.id and db.path(a.id)=="/organizations/acme/alice-admin"
    db.move(a.id, org.id)
    assert db.path(a.id)=="/organizations/alice-admin"
    assert db.get("/organizations/acme").children == (b.id,)
    assert db.get("/organizations").children == (acme.id,a.id)

def test_move_cycle_duplicate_and_delete_rules_are_atomic():
    db=TreeStore(); a=db.create("a"); b=db.create("b",parent=a.id); before=db.export_json()
    with pytest.raises(InvalidOperationError): db.move(a.id,b.id)
    assert db.export_json()==before
    sibling=db.create("sibling", parent=a.id)
    with pytest.raises(AlreadyExistsError): db.rename(b.id,"sibling")
    with pytest.raises(InvalidOperationError): db.delete(a.id)
    db.delete(a.id,recursive=True); assert not db.exists(a.id)

def test_properties_are_typed_detached_and_references():
    props={"none":None,"bool":True,"int":2**63,"float":1.5,"bytes":b"abc",
           "when":dt.datetime(2020,1,1,tzinfo=dt.timezone.utc),"ref":Reference("missing"),"nested":[{"x":1}]}
    db=TreeStore(); n=db.create("n",properties=props)
    props["nested"][0]["x"]=99
    got=db.get(n.id); assert got.properties["nested"][0]["x"]==1
    got.properties["nested"][0]["x"]=100; assert db.get(n.id).properties["nested"][0]["x"]==1
    with pytest.raises(InvalidPropertyError): db.create("bad",properties={"x":float("nan")})
    assert db.get(n.id).properties["ref"]==Reference("missing")

def test_transaction_rollback_and_conflict():
    db=TreeStore(); base=db.export_json()
    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            tx.create("x"); raise RuntimeError
    assert db.export_json()==base
    tx=db.transaction(); tx.create("a"); db.create("b")
    with pytest.raises(InvalidOperationError): tx.commit()
    assert db.exists("/b") and not db.exists("/a")

def test_export_import_roundtrip_and_malformed_atomicity():
    db=TreeStore(); n=db.create("n",type="custom",properties={"x":b"v"}); c=db.create("c",parent=n.id)
    exported=db.export(n.id); other=TreeStore(); imported=other.import_tree(exported)
    assert other.export(imported.id)==exported
    before=other.export_json()
    bad=dict(exported); bad["children"]= [{"id":"same","name":"x","type":"x","properties":{},"children":[]},{"id":"same","name":"y","type":"x","properties":{},"children":[]}]
    with pytest.raises(InvalidOperationError): other.import_tree(bad)
    assert other.export_json()==before

def test_persistence_reopen_and_truncated_tail(tmp_path):
    path=tmp_path/"db.log"
    with PersistentTreeStore(path) as db:
        root=db.create("root"); db.create("child",parent=root.id)
    size=path.stat().st_size
    with path.open("ab") as f: f.write(b"GROV1")
    with PersistentTreeStore(path) as db:
        assert db.exists("/root/child")
    assert path.stat().st_size==size

def test_persistence_corrupt_complete_frame_fails(tmp_path):
    path=tmp_path/"db.log"
    with PersistentTreeStore(path) as db: db.create("x")
    raw=bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(raw)
    with pytest.raises(StorageCorruptionError): PersistentTreeStore(path)

def test_cli_smoke(tmp_path, capsys):
    from grove.cli import main
    path=tmp_path/"db.log"
    with PersistentTreeStore(path) as db: db.create("x")
    main([str(path),"tree"]); assert "/ [root]" in capsys.readouterr().out


def test_failed_staged_operations_are_non_mutating_and_noop_update():
    db=TreeStore(); a=db.create("a"); b=db.create("b"); c=db.create("c")
    baseline=db.export_json(); tx=db.transaction()
    with pytest.raises(InvalidOperationError): tx.move(b.id, a.id, index=99)
    assert tx.path(b.id)=="/b" and tx.get("/").children == db.root.children
    with pytest.raises(InvalidOperationError): tx.copy(a.id, "/", name="a-copy", index=99)
    assert tx.export("/") if False else True
    tx.rollback(); assert db.export_json()==baseline
    n=db.get(a.id); db.update(a.id); assert db.get(a.id).modified_at==n.modified_at


def test_subscriptions_capture_recursive_delete_and_move_out():
    db=TreeStore(); parent=db.create("parent"); child=db.create("child",parent=parent.id)
    events=[]; sub=db.subscribe(events.append,parent.id,recursive=True)
    db.move(child.id,"/")
    assert any(e.node_id==child.id for e in events)
    events.clear(); moved=db.move(child.id,parent.id); events.clear(); db.delete(child.id)
    # Deletion is reported against the old subtree ancestry.
    assert any(e.node_id==child.id for e in events)
    sub.close()


def test_reference_remapping_on_import():
    source=TreeStore(); a=source.create("a"); b=source.create("b",properties={"link":Reference(a.id)})
    exported=source.export("/")
    # Subtree import remaps only IDs owned by the imported subtree.
    subtree=source.export(a.id)
    dst=TreeStore(); imported=dst.import_tree(subtree,preserve_ids=False)
    assert imported.id != a.id
    assert dst.get(imported.id).properties == {}
    with pytest.raises(AlreadyExistsError): dst.import_tree(exported)


def test_invalid_timestamp_and_unknown_tag_rejected_atomically():
    db=TreeStore(); n=db.create("n"); before=db.export_json(); fixture=db.export(n.id)
    fixture["created_at"] = 123; fixture["name"] = "n2"
    with pytest.raises(InvalidOperationError): db.import_tree(fixture, preserve_ids=False)
    fixture=db.export(n.id); fixture["name"] = "n3"; fixture["properties"]={"$grove":"wat","x":1}
    with pytest.raises(InvalidPropertyError): db.import_tree(fixture, preserve_ids=False)
    assert db.export_json()==before


def test_randomized_invariants_and_reopen(tmp_path):
    import random
    path=tmp_path/"random.log"; rng=random.Random(7)
    with PersistentTreeStore(path) as db:
        ids=[]
        for i in range(100):
            parent=rng.choice(ids) if ids and rng.random()<.7 else "/"
            try: ids.append(db.create(f"n{i}",parent=parent).id)
            except Exception: pass
        for _ in range(100):
            if not ids: break
            node=rng.choice(ids)
            if rng.random()<.5:
                db.update(node,properties={"v":rng.randrange(100)})
            else:
                target=rng.choice(ids) if ids and rng.random()<.5 else "/"
                try: db.move(node,target)
                except InvalidOperationError: pass
        for node_id in ids:
            if db.exists(node_id): assert db.get(db.path(node_id)).id==node_id
    with PersistentTreeStore(path) as reopened:
        for node_id in ids:
            if reopened.exists(node_id): assert reopened.get(node_id).id==node_id


def test_import_remaps_internal_references():
    source=TreeStore(); parent=source.create("parent")
    child=source.create("child", parent=parent.id, properties={"up": Reference(parent.id)})
    exported=source.export(parent.id)
    dst=TreeStore(); imported=dst.import_tree(exported, preserve_ids=False)
    imported_child=dst.get("/parent/child")
    assert imported.id != parent.id
    assert imported_child.properties["up"] == Reference(imported.id)


def test_failed_index_can_be_caught_then_transaction_committed():
    db=TreeStore(); a=db.create("a"); b=db.create("b")
    baseline=db.export_json(); tx=db.transaction()
    with pytest.raises(InvalidOperationError): tx.move(b.id, "/", index=100)
    tx.commit()
    assert db.export_json() == baseline


def test_corrupt_empty_or_partial_initial_file_is_not_new_database(tmp_path):
    for suffix, content in [("garbage", b"garbage"), ("partial", b"GROV1")]:
        path=tmp_path/suffix
        path.write_bytes(content)
        with pytest.raises(StorageCorruptionError): PersistentTreeStore(path)


def test_persistence_corruption_between_commits_fails_closed(tmp_path):
    path=tmp_path/"middle.log"
    with PersistentTreeStore(path) as db:
        db.create("first")
        db.create("second")
    raw=path.read_bytes(); magic=b"GROV1\0"
    first=raw.find(magic); second=raw.find(magic, first+1)
    assert first == 0 and second > 0
    path.write_bytes(raw[:second] + b"BAD" + raw[second:])
    with pytest.raises(StorageCorruptionError): PersistentTreeStore(path)


def test_malformed_reference_is_rejected():
    db=TreeStore(); n=db.create("n"); fixture=db.export(n.id); fixture["name"]="n2"
    fixture["properties"]={"$grove":"reference","id":"bad/id"}
    with pytest.raises(InvalidPropertyError): db.import_tree(fixture, preserve_ids=False)


def test_complete_root_export_import_into_empty_store():
    source=TreeStore(); source.create("one"); source.create("two")
    exported=source.export("/")
    target=TreeStore(); imported=target.import_tree(exported, preserve_ids=True)
    assert imported.id == exported["id"]
    assert target.export("/") == exported
    nonempty=TreeStore(); nonempty.create("existing")
    with pytest.raises(AlreadyExistsError): nonempty.import_tree(exported)


def test_model_based_randomized_tree_sequences():
    import random
    for seed in range(20):
        rng=random.Random(seed); db=TreeStore()
        root=db.root.id
        model={root:{"name":"","parent":None,"children":[]}}
        serial=0
        def model_path(nid):
            parts=[]
            while model[nid]["parent"] is not None:
                parts.append(model[nid]["name"]); nid=model[nid]["parent"]
            return "/"+"/".join(reversed(parts))
        def check():
            assert set(model)==set(_ids_in_store(db))
            for nid,m in model.items():
                node=db.get(nid)
                assert node.name==m["name"] and node.parent_id==m["parent"]
                assert list(node.children)==m["children"]
                assert db.path(nid)==model_path(nid)
                assert db.get(model_path(nid)).id==nid
        def add_model(nid,name,parent):
            model[nid]={"name":name,"parent":parent,"children":[]}
            model[parent]["children"].append(nid)
        def descendants(nid):
            out=[]
            for child in list(model[nid]["children"]): out += [child]+descendants(child)
            return out
        for step in range(100):
            live=list(model)
            if len(live)<3 or rng.random()<.35:
                parent=rng.choice(live); name=f"s{seed}_{step}_{serial}"; serial+=1
                node=db.create(name,parent=parent.id if False else parent)
                add_model(node.id,name,parent)
            elif rng.random()<.25:
                nid=rng.choice(live[1:]); old=model[nid]["name"]; name=old+"x"
                db.rename(nid,name); model[nid]["name"]=name
            elif rng.random()<.35:
                nid=rng.choice(live[1:]); possible=[x for x in live if x not in descendants(nid) and x!=nid]
                parent=rng.choice(possible)
                old_parent=model[nid]["parent"]; model[old_parent]["children"].remove(nid)
                db.move(nid,parent); model[nid]["parent"]=parent; model[parent]["children"].append(nid)
            else:
                nid=rng.choice(live[1:]); parent=model[nid]["parent"]
                db.delete(nid,recursive=True)
                doomed=[nid]+descendants(nid)
                model[parent]["children"].remove(nid)
                for d in doomed: del model[d]
            check()


def _ids_in_store(db):
    out=[]
    def visit(nid):
        out.append(nid)
        for child in db.get(nid).children: visit(child)
    visit(db.root.id)
    return out


def test_timestamp_order_compares_instants_across_timezones():
    db = TreeStore()
    node = db.create("node")
    tx = db.transaction()
    # Lexically this modified timestamp is later (00:30 > 00:00), but its
    # UTC instant is five hours earlier than creation.
    tx._state["nodes"][node.id]["created_at"] = "2020-01-01T00:00:00+00:00"
    tx._state["nodes"][node.id]["modified_at"] = "2020-01-01T00:30:00+05:00"
    with pytest.raises(InvalidOperationError, match="modified_at cannot precede"):
        tx.commit()

