#!/usr/bin/env python3
"""Snapshot and compare the AST of every function/class body across a package.

This is the "structural oracle" for a split: if the only thing that happened
was relocation, the multiset of (qualified name -> AST hash) is identical
before and after. Dropped code shows up as MISSING, invented code as ADDED,
edited code as CHANGED.

Usage:
    snapshot_bodies.py snapshot <path> [<path>...] --out before.json
    snapshot_bodies.py compare before.json <path> [<path>...] [--strict] [--allow-changed name,...]

<path> may be files or directories (recursed, .py only).
Exit codes: 0 identical (or only allowed diffs), 1 differences found, 2 usage error.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", ".tox", ".mypy_cache"}


def iter_py_files(paths):
    for p in paths:
        if os.path.isfile(p) and p.endswith(".py"):
            yield p
        elif os.path.isdir(p):
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in sorted(filenames):
                    if fn.endswith(".py"):
                        yield os.path.join(dirpath, fn)


class _Normalizer(ast.NodeTransformer):
    """Strip docstrings so docstring edits don't count as behavior changes."""

    def _strip_doc(self, node):
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
        return node

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        return self._strip_doc(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        return self._strip_doc(node)


def body_hashes(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        print(f"SYNTAX ERROR in {path}: {e}", file=sys.stderr)
        return {"__SYNTAX_ERROR__::" + path: "error"}
    tree = _Normalizer().visit(tree)
    ast.fix_missing_locations(tree)
    out = {}

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = f"{prefix}{child.name}"
                dumped = ast.dump(child, include_attributes=False)
                h = hashlib.sha256(dumped.encode()).hexdigest()[:16]
                out[qual] = h
                if isinstance(child, ast.ClassDef):
                    visit(child, qual + ".")
    visit(tree, "")
    return out


def collect(paths):
    """Return {qualname: [hash, ...]} (list, because a name may legitimately
    appear in more than one module, e.g. two modules each define `main`)."""
    result: dict[str, list[str]] = {}
    where: dict[str, list[str]] = {}
    for p in iter_py_files(paths):
        for q, h in body_hashes(p).items():
            result.setdefault(q, []).append(h)
            where.setdefault(q, []).append(p)
    for q in result:
        result[q].sort()
    return result, where


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("paths", nargs="+")
    s.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("paths", nargs="+")
    c.add_argument("--strict", action="store_true", help="fail on CHANGED too (default: fail only on MISSING/ADDED)")
    c.add_argument("--allow-changed", default="", help="comma-separated qualnames allowed to change")
    c.add_argument("--manifest", help="decompose manifest; verify complete declared structural transformation")
    c.add_argument("--tier3", action="store_true", help="reviewed design change; enables method-only allow-list")
    c.add_argument("--base", help="committed baseline for explicit --tier3 verification")
    args = ap.parse_args()

    if args.cmd == "snapshot":
        hashes, where = collect(args.paths)
        with open(args.out, "w", encoding="utf-8") as f:
            files = {}
            for p in iter_py_files(args.paths):
                with open(p, encoding="utf-8") as source:
                    files[os.path.normpath(os.path.relpath(p))] = hashlib.sha256(source.read().encode()).hexdigest()
            json.dump({"hashes": hashes, "where": where, "files": files,
                       "paths": [os.path.normpath(os.path.relpath(p)) for p in args.paths]}, f, indent=1, sort_keys=True)
        print(f"snapshot: {sum(len(v) for v in hashes.values())} bodies across {len(where)} names -> {args.out}")
        return 0

    with open(args.before, encoding="utf-8") as f:
        snapshot = json.load(f)
        before = snapshot["hashes"]
    if args.manifest or args.tier3:
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'decompose' / 'scripts'))
        from common import Refusal, git_sources
        from manifest_oracle import verify, verify_scope, verify_allowed_methods
        try:
            current = {os.path.normpath(os.path.relpath(p)): Path(p).read_text() for p in iter_py_files(args.paths)}
            if args.manifest:
                if args.allow_changed or args.tier3:
                    raise Refusal('manifest operations cannot waive body changes')
                manifest = json.loads(Path(args.manifest).read_text())
                touched = set(manifest['baseline'])
                verify_scope(snapshot, current, touched)
                if any(snapshot['files'].get(p, hashlib.sha256(b'').hexdigest()) != manifest['baseline'][p] for p in touched):
                    raise Refusal('manifest baseline differs from snapshot')
                originals = git_sources('.', touched, manifest['base_commit'])
                verify(originals, {p: current[p] for p in touched}, manifest)
                print(f'manifest oracle: PASS (tier {manifest["tier"]})')
            else:
                if not args.base:
                    raise Refusal('--tier3 requires --base <committed baseline>')
                verify_scope(snapshot, current, set(current))
                originals = git_sources('.', current, args.base)
                if any(hashlib.sha256(s.encode()).hexdigest() != snapshot['files'].get(p) for p, s in originals.items()):
                    raise Refusal('tier 3 baseline differs from snapshot')
                verify_allowed_methods(originals, current, args.allow_changed.split(','))
                print('tier 3 method allow-list: PASS; tests and review still required')
            return 0
        except (Refusal, ValueError, KeyError, SyntaxError) as exc:
            print(f'decompose oracle: FAIL: {exc}', file=sys.stderr)
            return 1
    after, where = collect(args.paths)
    allow = {x.strip() for x in args.allow_changed.split(",") if x.strip()}

    missing = sorted(q for q in before if q not in after)
    added = sorted(q for q in after if q not in before)
    changed = sorted(q for q in before if q in after and before[q] != after[q] and q not in allow)
    syntax = [q for q in after if q.startswith("__SYNTAX_ERROR__")]

    for q in missing:
        print(f"MISSING  {q}")
    for q in added:
        print(f"ADDED    {q}  ({', '.join(where.get(q, []))})")
    for q in changed:
        print(f"CHANGED  {q}  ({', '.join(where.get(q, []))})")
    if syntax:
        print("SYNTAX ERRORS present; see stderr")
    print(f"summary: {len(before)} before, {len(after)} after, "
          f"missing={len(missing)} added={len(added)} changed={len(changed)}")

    bad = bool(missing or added or syntax) or (args.strict and bool(changed))
    if changed and not args.strict:
        print("note: CHANGED entries need reviewer sign-off (run with --strict to fail on them)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
