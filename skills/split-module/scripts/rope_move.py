#!/usr/bin/env python3
"""Move top-level symbols between modules with rope (imports updated repo-wide).

This is the ONLY sanctioned way for an agent to relocate code during a split.
It never rewrites function bodies; it cuts the definition, pastes it into the
destination, and rewrites every import/reference in the project.

Usage:
    rope_move.py --project . --source pkg/big.py --dest pkg/models.py --symbols ClassA,helper_b [--apply]

Without --apply it prints the change description (dry run) and exits 0.
With --apply it applies the changes and exits 0; exits 1 on any failure
(nothing is written if a symbol fails; symbols are moved one at a time and the
script stops at the first failure so the tree is never half-moved without a
clear message).

Resources rope must not see (importers that re-exports keep valid, files its parser
cannot read) can be excluded with --ignore glob[,glob...] and/or one glob per line in
<project>/.refactor/rope-ignore.txt (or rope-ignore); both extend IGNORED.

Requires: pip install rope
"""
from __future__ import annotations

import argparse
import ast
import os
import sys

try:
    from rope.base import libutils
    from rope.base.project import Project
    from rope.refactor.move import create_move
except ImportError:
    print("rope is not installed. Run: pip install rope   (or: uv pip install rope)", file=sys.stderr)
    sys.exit(3)

IGNORED = [".git", ".venv", "venv", "node_modules", "build", "dist", "*.egg-info", ".tox", ".mypy_cache", "__pycache__"]


def symbol_offset(src: str, name: str) -> int:
    """Byte offset of the identifier in the symbol's definition line."""
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            line_start = sum(len(l) + 1 for l in src.splitlines(keepends=False)[: node.lineno - 1])
            # rope uses character offsets; find the name on the def/class line
            line = src.splitlines()[node.lineno - 1]
            col = line.find(name, node.col_offset)
            if col < 0:
                col = line.find(name)
            return line_start + col
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    line_start = sum(len(l) + 1 for l in src.splitlines()[: node.lineno - 1])
                    return line_start + t.col_offset
    raise SystemExit(f"symbol {name!r} is not a top-level function/class/assignment in the source module")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=".")
    ap.add_argument("--source", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--symbols", required=True, help="comma-separated top-level names, moved in order")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--ignore", default="", help="comma-separated extra globs rope must not see (see also .refactor/rope-ignore)")
    args = ap.parse_args()

    root = os.path.abspath(args.project)
    src_path = os.path.abspath(args.source)
    dst_path = os.path.abspath(args.dest)
    if not src_path.startswith(root) or not dst_path.startswith(root):
        print("source and dest must be inside --project", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    if not os.path.exists(dst_path):
        with open(dst_path, "w", encoding="utf-8") as f:
            f.write('"""Extracted from %s."""\n' % os.path.relpath(src_path, root))
        print(f"created {os.path.relpath(dst_path, root)}")

    ignored = list(IGNORED) + [g.strip() for g in args.ignore.split(",") if g.strip()]
    for ignore_name in ("rope-ignore.txt", "rope-ignore"):
        ignore_file = os.path.join(root, ".refactor", ignore_name)
        if os.path.exists(ignore_file):
            with open(ignore_file, encoding="utf-8") as f:
                ignored += [line.strip() for line in f if line.strip() and not line.startswith("#")]
            break
    project = Project(root, ropefolder=None, ignored_resources=ignored, ignore_syntax_errors=True)
    try:
        for name in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            project.validate()
            src_res = libutils.path_to_resource(project, src_path)
            dst_res = libutils.path_to_resource(project, dst_path)
            src_text = src_res.read()
            offset = symbol_offset(src_text, name)
            mover = create_move(project, src_res, offset)
            changes = mover.get_changes(dst_res)
            desc = changes.get_description()
            touched = sorted({c.get_changed_resources() and list(c.get_changed_resources())[0].path
                              for c in changes.changes if c.get_changed_resources()} - {None})
            print(f"=== move {name}: {os.path.relpath(src_path, root)} -> {os.path.relpath(dst_path, root)}")
            print(f"files touched: {', '.join(touched)}")
            if not args.apply:
                print(desc[:4000] + ("\n... (truncated)" if len(desc) > 4000 else ""))
                continue
            project.do(changes)
            print(f"applied: {name}")
    except Exception as e:  # rope raises many specific exception types
        print(f"MOVE FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        print("Nothing further was applied. Fix the cause (usually: symbol has module-level "
              "dependencies rope cannot resolve, or a name clash in dest) and re-run.", file=sys.stderr)
        return 1
    finally:
        project.close()
    if not args.apply:
        print("(dry run; re-run with --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
