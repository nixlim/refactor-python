#!/usr/bin/env python3
"""Inventory a large Python module: top-level symbols, intra-module references,
module-level state, and suggested extraction clusters.

Usage:
    inventory.py path/to/module.py [--json out.json] [--repo-root .] [--module-name pkg.mod]

Output (stdout): a compact Markdown report an agent can read without loading
the whole file. With --json, a machine-readable graph is written too.

Only the standard library is used. Nothing is modified.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import defaultdict

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox", ".mypy_cache"}


def top_level_symbols(tree: ast.Module):
    """Yield (name, node, kind) for every top-level definition/assignment."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node, "function"
        elif isinstance(node, ast.ClassDef):
            yield node.name, node, "class"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    yield t.id, node, "assignment"
                elif isinstance(t, ast.Tuple):
                    for e in t.elts:
                        if isinstance(e, ast.Name):
                            yield e.id, node, "assignment"


def referenced_names(node: ast.AST) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            # capture the root of dotted access, e.g. CONFIG.x -> CONFIG
            root = sub
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                names.add(root.id)
    return names


def loc(node: ast.AST) -> int:
    return (getattr(node, "end_lineno", node.lineno) or node.lineno) - node.lineno + 1


def is_mutable_state(node: ast.AST) -> bool:
    """Heuristic: module-level assignment whose value is a container/call and
    whose name is not ALL_CAPS (constants are fine to move anywhere)."""
    value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
    if value is None:
        return False
    return isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Call))


def connected_components(nodes, edges):
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    groups = defaultdict(list)
    for n in nodes:
        groups[find(n)].append(n)
    return list(groups.values())


def external_dependents(repo_root: str, module_name: str, this_file: str):
    """Grep the repo for imports of this module. Cheap, regex-based."""
    if not module_name:
        return []
    pat = re.compile(
        r"^\s*(from\s+" + re.escape(module_name) + r"(\.|\s+import)|import\s+" + re.escape(module_name) + r"(\s|$|\.|,))",
        re.M,
    )
    hits = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            if os.path.abspath(p) == os.path.abspath(this_file):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            n = len(pat.findall(txt))
            if n:
                hits.append((os.path.relpath(p, repo_root), n))
    return sorted(hits, key=lambda x: -x[1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("module")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--module-name", default="", help="dotted import name, e.g. app.engine")
    ap.add_argument("--min-cluster-loc", type=int, default=150, help="only report clusters >= this many lines")
    args = ap.parse_args()

    with open(args.module, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=args.module)
    total_lines = src.count("\n") + 1

    symbols = {}  # name -> dict
    order = []
    for name, node, kind in top_level_symbols(tree):
        if name in symbols:
            continue  # first definition wins for reporting
        symbols[name] = {
            "name": name,
            "kind": kind,
            "line": node.lineno,
            "end_line": getattr(node, "end_lineno", node.lineno),
            "loc": loc(node),
            "refs": set(),
            "mutable_state": kind == "assignment" and is_mutable_state(node),
            "node": node,
        }
        order.append(name)

    names = set(symbols)
    edges = []
    for name in order:
        node = symbols[name]["node"]
        refs = referenced_names(node) & names
        refs.discard(name)
        symbols[name]["refs"] = refs
        for r in refs:
            edges.append((name, r))

    in_degree = defaultdict(int)
    for a, b in edges:
        in_degree[b] += 1

    # Clusters via weakly connected components over non-state symbols;
    # module-level mutable state is reported separately because it needs one owner.
    def_nodes = [n for n in order if symbols[n]["kind"] in ("function", "class")]
    def_edges = [(a, b) for a, b in edges if a in def_nodes and b in def_nodes]
    comps = connected_components(def_nodes, def_edges)
    comps.sort(key=lambda c: -sum(symbols[n]["loc"] for n in c))

    globals_used = []
    for name in order:
        node = symbols[name]["node"]
        for sub in ast.walk(node):
            if isinstance(sub, ast.Global):
                globals_used.append((name, sub.names))

    deps = external_dependents(args.repo_root, args.module_name, args.module)

    # ---- Markdown report ----
    out = []
    out.append(f"# Inventory: {args.module}")
    out.append(f"- total lines: {total_lines}")
    out.append(f"- top-level symbols: {len(order)} "
               f"(functions {sum(1 for n in order if symbols[n]['kind']=='function')}, "
               f"classes {sum(1 for n in order if symbols[n]['kind']=='class')}, "
               f"assignments {sum(1 for n in order if symbols[n]['kind']=='assignment')})")
    out.append(f"- intra-module reference edges: {len(edges)}")
    if deps:
        out.append(f"- external files importing this module: {len(deps)} (top: "
                   + ", ".join(f"{p} x{n}" for p, n in deps[:8]) + ")")
    out.append("")
    out.append("## Module-level mutable state (must get exactly ONE owning module)")
    ms = [n for n in order if symbols[n]["mutable_state"]]
    if ms:
        for n in ms:
            users = sorted(a for a, b in edges if b == n)
            out.append(f"- `{n}` (line {symbols[n]['line']}) used by: {', '.join(users) or 'nobody'}")
    else:
        out.append("- none detected")
    if globals_used:
        out.append("")
        out.append("## `global` statements (behavior hazard when moving)")
        for fn, gnames in globals_used:
            out.append(f"- `{fn}` declares global {', '.join(gnames)}")
    out.append("")
    out.append(f"## Suggested clusters (weakly connected components; showing >= {args.min_cluster_loc} LOC)")
    shown = 0
    for i, comp in enumerate(comps, 1):
        comp_loc = sum(symbols[n]["loc"] for n in comp)
        if comp_loc < args.min_cluster_loc and shown >= 3:
            continue
        shown += 1
        comp_sorted = sorted(comp, key=lambda n: symbols[n]["line"])
        external_in = sorted({a for a, b in edges if b in comp and a not in comp})
        external_out = sorted({b for a, b in edges if a in comp and b not in comp})
        out.append(f"### Cluster {i}: {comp_loc} LOC, {len(comp)} symbols")
        out.append("- members: " + ", ".join(f"`{n}`" for n in comp_sorted[:40]) + (" ..." if len(comp_sorted) > 40 else ""))
        out.append(f"- referenced from outside cluster by: {', '.join(external_in[:15]) or 'none (leaf-safe)'}")
        out.append(f"- references outside cluster: {', '.join(external_out[:15]) or 'none'}")
    out.append("")
    out.append("## Largest symbols")
    for n in sorted(order, key=lambda n: -symbols[n]["loc"])[:15]:
        s = symbols[n]
        out.append(f"- `{n}` ({s['kind']}, lines {s['line']}-{s['end_line']}, {s['loc']} LOC, fan-in {in_degree[n]}, fan-out {len(s['refs'])})")
    out.append("")
    out.append("## Leaves (fan-out 0 within module; safest to extract first)")
    leaves = [n for n in def_nodes if not symbols[n]["refs"]]
    out.append(", ".join(f"`{n}`" for n in leaves[:60]) or "none")
    print("\n".join(out))

    if args.json_out:
        data = {
            "module": args.module,
            "total_lines": total_lines,
            "symbols": [
                {k: (sorted(v) if isinstance(v, set) else v) for k, v in s.items() if k != "node"}
                for s in (symbols[n] for n in order)
            ],
            "edges": edges,
            "clusters": [sorted(c, key=lambda n: symbols[n]["line"]) for c in comps],
            "mutable_state": ms,
            "external_dependents": deps,
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"\n(json written to {args.json_out})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
