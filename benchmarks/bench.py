"""Reproducible GROVE vertical-slice smoke benchmark.

Example: ``uv run python benchmarks/bench.py --nodes 1000 --reads 1000``.
The output is JSON so it can be checked into an experiment artifact.
"""
from __future__ import annotations
import argparse, json, os, platform, random, statistics, sys, tempfile, time
from pathlib import Path
from grove import PersistentTreeStore, TreeStore

def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=1000)
    parser.add_argument("--reads", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    args=parser.parse_args()
    rng=random.Random(args.seed)
    db=TreeStore()
    t0=time.perf_counter()
    with db.transaction() as tx:
        ids=[]
        for i in range(args.nodes):
            parent=rng.choice(ids) if ids and rng.random() < .25 else "/"
            ids.append(tx.create(f"n{i}", parent=parent).id)
    build_ms=(time.perf_counter()-t0)*1000
    read_times=[]
    for _ in range(args.reads):
        target=rng.choice(ids)
        t=time.perf_counter(); db.get(target); read_times.append((time.perf_counter()-t)*1e6)
    t=time.perf_counter(); exported=db.export_json(); export_ms=(time.perf_counter()-t)*1000
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/"grove.log"
        durable=PersistentTreeStore(path)
        t=time.perf_counter()
        with durable.transaction() as tx:
            # Importing a complete tree measures a realistic single commit.
            tx.import_tree(db.export("/"), preserve_ids=False)
        durable_ms=(time.perf_counter()-t)*1000
        durable_bytes=path.stat().st_size
    result={"seed":args.seed,"nodes":args.nodes,"reads":args.reads,
      "machine":platform.platform(),"python":sys.version.split()[0],
      "build_ms":round(build_ms,3),"export_ms":round(export_ms,3),
      "export_bytes":len(exported.encode()),"read_p50_us":round(statistics.median(read_times),3),
      "read_p95_us":round(sorted(read_times)[max(0,int(.95*len(read_times))-1)],3),
      "durable_import_commit_ms":round(durable_ms,3),"durable_bytes":durable_bytes}
    print(json.dumps(result, sort_keys=True, indent=2))
if __name__ == "__main__": main()
