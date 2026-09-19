"""Shared, dependency-free parsing and worktree publishing helpers."""
from __future__ import annotations

import ast
import builtins
import difflib
import hashlib
import json
import os
import subprocess
import symtable
from pathlib import Path

SKIP = {'.git', '.venv', 'venv', '__pycache__', 'node_modules', '.worktrees'}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


class Refusal(ValueError):
    """An operation outside the mechanically verifiable subset."""


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def dump(node):
    return ast.dump(node, include_attributes=False)


def resolve(root, name):
    path = (Path(root) / name).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise Refusal(f'path outside project: {name}')
    return path


def definition(tree, qualname):
    node = tree
    for name in qualname.split('.'):
        matches = [n for n in node.body if isinstance(n, (*FUNCTIONS, ast.ClassDef)) and n.name == name]
        if len(matches) != 1:
            raise Refusal(f'expected exactly one definition: {qualname} (found {len(matches)})')
        node = matches[0]
    return node


def module_name(path):
    parts = Path(path).with_suffix('').parts
    if parts[-1] == '__init__':
        parts = parts[:-1]
    if not parts or not all(p.isidentifier() for p in parts):
        raise Refusal('destination must have an importable module path (use --import-root)')
    return '.'.join(parts)


def global_names(source, qualname):
    table = symtable.symtable(source, '<source>', 'exec')
    module_bound = {s.get_name() for s in table.get_symbols() if s.is_assigned() or s.is_imported()}
    for name in qualname.split('.'):
        children = [c for c in table.get_children() if c.get_name() == name]
        if len(children) != 1:
            raise Refusal(f'ambiguous scope: {qualname}')
        table = children[0]
    def walk(t):
        names = {s.get_name() for s in t.get_symbols() if s.is_global() and s.is_referenced()}
        for child in t.get_children():
            names |= walk(child)
        return names
    return walk(table) - (set(dir(builtins)) - module_bound)


def files(root):
    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(names):
            if name.endswith('.py'):
                yield Path(current) / name


def read_sources(root, paths):
    if any(Path(p).suffix != '.py' for p in paths):
        raise Refusal('source and destination must be Python .py files')
    if len({resolve(root, p) for p in paths}) != len(paths):
        raise Refusal('source/destination paths alias the same file')
    return {p: resolve(root, p).read_text() if resolve(root, p).exists() else '' for p in paths}


def git_sources(root, paths, base):
    """Read only the touched baseline files, never embed them in evidence JSON."""
    sources = {}
    for path in paths:
        resolve(root, path)
        result = subprocess.run(['git', 'show', f'{base}:{path}'], cwd=root,
                                capture_output=True, text=True, check=False)
        if result.returncode:
            # Absent destinations are allowed; an invalid commit is not.
            commit = subprocess.run(['git', 'cat-file', '-e', f'{base}^{{commit}}'], cwd=root,
                                    capture_output=True, check=False)
            if commit.returncode:
                raise Refusal(f'baseline commit is unavailable: {base}')
        sources[path] = result.stdout if result.returncode == 0 else ''
    return sources


def publish(root, before, after, manifest, manifest_path, apply=False):
    """Validate before writing. Apply runs in a git worktree with committed inputs.

    On I/O failure leave the diff for inspection/recovery using git; never revert
    other work automatically. Dry runs write nothing, including the manifest.
    """
    from manifest_oracle import verify
    verify(before, after, manifest)
    for path in before:
        target = resolve(root, path)
        actual = target.read_text() if target.exists() else ''
        if actual != before[path]:
            raise Refusal(f'stale source: {path}')
    evidence = resolve(root, manifest_path)
    if evidence.exists() or evidence in [resolve(root, p) for p in after]:
        raise Refusal('manifest output already exists or overlaps source')
    if not apply:
        for p in after:
            print(''.join(difflib.unified_diff(before[p].splitlines(True), after[p].splitlines(True),
                                             fromfile=p, tofile=p)), end='')
        print(json.dumps(manifest, indent=2))
        print('DRY RUN: verified; no files written')
        return
    commit = subprocess.run(['git', 'rev-parse', '--verify', 'HEAD'], cwd=root,
                            capture_output=True, text=True, check=False)
    if commit.returncode or git_sources(root, before, commit.stdout.strip()) != before:
        raise Refusal('commit the source/destination baseline before --apply')
    manifest['base_commit'] = commit.stdout.strip()
    for path, text in after.items():
        target = resolve(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'APPLIED tier {manifest["tier"]}; manifest: {manifest_path}')
