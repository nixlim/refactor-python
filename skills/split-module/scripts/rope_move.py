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

After a move rope rewrites the remaining references in the SOURCE module to the fully
qualified destination name (`pkg.mod.dest.name`) and adds `import pkg.mod.dest`. For a
split whose source is a package root that re-exports everything, that changes every
referencing body. By default this script restores bare names in the source and replaces
rope's import with `from pkg.mod.dest import name, ...` (an import-line edit only), so the
body-hash oracle sees no change; pass --keep-qualified-refs to keep rope's output.

Requires: pip install rope
"""
from __future__ import annotations

import argparse
import ast
import os
import re
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
        if isinstance(node, ast.AnnAssign):
            t = node.target
            if isinstance(t, ast.Name) and t.id == name:
                line_start = sum(len(l) + 1 for l in src.splitlines()[: node.lineno - 1])
                return line_start + t.col_offset
    raise SystemExit(f"symbol {name!r} is not a top-level function/class/assignment in the source module")


def restore_bare_refs(src_path: str, dest_mod: str, moved: list[str]) -> None:
    """Undo rope's qualification of the moved names inside the source module.

    rope rewrites `name(...)` to `pkg.mod.dest.name(...)` in every remaining body of the
    source and adds `import pkg.mod.dest`. Restore the bare names and turn that import into
    `from pkg.mod.dest import name, ...`, so only import lines change and the body-hash
    oracle stays clean.

    Only the names in `moved` are de-qualified (word-bounded regex), never a blanket
    `pkg.mod.dest.` strip: a qualified reference to some *other* member of the destination
    (from an earlier batch run with --keep-qualified-refs, or a docstring mention) must
    keep working. If such references remain, rope's `import pkg.mod.dest` is kept and the
    from-import is added next to it. Validated with ast.parse before anything is written.
    """
    with open(src_path, encoding="utf-8") as f:
        text = f.read()
    pattern = re.compile(r"\b" + re.escape(dest_mod) + r"\.(" + "|".join(re.escape(n) for n in moved) + r")\b")
    text, count = pattern.subn(r"\1", text)
    remaining = len(re.findall(r"\b" + re.escape(dest_mod) + r"\.", text))
    import_line = f"import {dest_mod}\n"
    from_block = f"from {dest_mod} import (\n" + "".join(f"    {n},\n" for n in moved) + ")\n"
    if import_line in text:
        text = text.replace(import_line, (import_line if remaining else "") + from_block, 1)
    elif count:
        raise RuntimeError(f"source has {count} qualified references but no `import {dest_mod}` line")
    ast.parse(text)
    with open(src_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"source: restored {count} bare reference(s) to {', '.join(moved)}; "
          + (f"`import {dest_mod}` kept ({remaining} other qualified use(s)) and " if remaining else f"`import {dest_mod}` -> ")
          + "from-import added")


def _reexport_owners(src_text: str, src_mod: str) -> dict[str, str]:
    """{name: owner module} for every `from <src_mod>.<sub> import ...` in the source root."""
    owners: dict[str, str] = {}
    for node in ast.parse(src_text).body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(src_mod + "."):
            for alias in node.names:
                owners[alias.asname or alias.name] = node.module
    return owners


def fix_dest_imports(dst_path: str, src_path: str, src_mod: str, moved: list[str]) -> None:
    """Repair the `from <src_mod> import ...` line rope writes into the destination.

    rope imports every source global the moved code might need, including names that now
    live in the destination itself (a circular import at load) and names the source root
    merely re-exports from earlier-extracted submodules. Drop the self-imports and point
    the re-exported names at their owner module; names still defined in the source root
    stay as they are (the plan's wave order is what keeps those from cycling). Import
    lines only; validated with ast.parse before writing.
    """
    with open(dst_path, encoding="utf-8") as f:
        text = f.read()
    with open(src_path, encoding="utf-8") as f:
        owners = _reexport_owners(f.read(), src_mod)
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    edits = []  # (start_line_index, end_line_index_exclusive, replacement_text)
    for node in tree.body:
        if not (isinstance(node, ast.ImportFrom) and node.module == src_mod and node.level == 0):
            continue
        keep, retarget = [], {}
        for alias in node.names:
            if alias.name in moved:
                continue  # defined here now
            owner = owners.get(alias.name)
            if owner:
                retarget.setdefault(owner, []).append(alias.name)
            else:
                keep.append(alias.name)
        out = ""
        if keep:
            out += f"from {src_mod} import (\n" + "".join(f"    {n},\n" for n in keep) + ")\n"
        for owner, names in sorted(retarget.items()):
            out += f"from {owner} import (\n" + "".join(f"    {n},\n" for n in names) + ")\n"
        edits.append((node.lineno - 1, node.end_lineno, out))
    for start, end, out in reversed(edits):
        lines[start:end] = [out]
    new_text = "".join(lines)
    ast.parse(new_text)
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"dest: {len(edits)} source import line(s) repaired (self-imports dropped, re-exported names pointed at their owner modules)")


def reorder_dest(dst_path: str, moved: list[str]) -> None:
    """Put the moved top-level statements in the order they were requested.

    rope inserts each moved definition at the top of the destination, so a sequence of
    moves lands in reverse order and an alias like `B = A` ends up above `A = ...`.
    Whole statements only (decorators and directly attached leading comments travel
    with their statement); no body or reference is touched; validated with ast.parse.
    """
    with open(dst_path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    spans: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        names: list[str] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [node.name]
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        hit = [n for n in names if n in moved]
        if not hit:
            continue
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        while start > 0 and lines[start - 1].lstrip().startswith("#"):
            start -= 1
        spans[hit[0]] = (start, node.end_lineno)
    if len(spans) != len(moved):
        print(f"dest: order left as is ({len(spans)} of {len(moved)} moved names found as top-level statements)")
        return
    order_now = sorted(spans, key=lambda n: spans[n][0])
    if order_now == moved:
        return
    blocks = {n: "".join(lines[a:b]) for n, (a, b) in spans.items()}
    cut = set()
    for a, b in spans.values():
        cut.update(range(a, b))
    keep = [l for i, l in enumerate(lines) if i not in cut]
    body = "".join(keep).rstrip("\n") + "\n"
    for n in moved:
        block = blocks[n].rstrip("\n") + "\n"
        body += "\n\n" + block
    ast.parse(body)
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"dest: {len(moved)} moved statement(s) reordered into the requested (dependency) order")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=".")
    ap.add_argument("--source", required=True)
    ap.add_argument("--dest", required=True)
    ap.add_argument("--symbols", required=True, help="comma-separated top-level names, moved in order")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--keep-qualified-refs", action="store_true", help="keep rope's pkg.mod.dest.name references in the source module (default: restore bare names + from-import)")
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
    moved: list[str] = []
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
            moved.append(name)
            print(f"applied: {name}")
        if moved and not args.keep_qualified_refs:
            src_mod = libutils.modname(libutils.path_to_resource(project, src_path))
            dst_mod = libutils.modname(libutils.path_to_resource(project, dst_path))
            restore_bare_refs(src_path, dst_mod, moved)
            fix_dest_imports(dst_path, src_path, src_mod, moved)
            reorder_dest(dst_path, moved)
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
