"""Small command-line tree inspector."""
from __future__ import annotations
import argparse, json
from .store import PersistentTreeStore

def _print_tree(store, node_id, prefix=""):
    node=store.get(node_id)
    label=node.name or "/"
    print(prefix + label + f" [{node.type}] <{node.id}>")
    for i, child in enumerate(node.children):
        _print_tree(store, child, prefix + "  ")

def main(argv=None):
    parser=argparse.ArgumentParser(prog="grove")
    parser.add_argument("database")
    sub=parser.add_subparsers(dest="command", required=True)
    show=sub.add_parser("tree")
    show.add_argument("path", nargs="?", default="/")
    get=sub.add_parser("get")
    get.add_argument("target")
    exp=sub.add_parser("export")
    exp.add_argument("path", nargs="?", default="/")
    args=parser.parse_args(argv)
    with PersistentTreeStore(args.database) as store:
        if args.command == "tree": _print_tree(store, args.path)
        elif args.command == "get": print(json.dumps(store.export(args.target), ensure_ascii=False, indent=2))
        elif args.command == "export": print(store.export_json(args.path))

if __name__ == "__main__": main()
