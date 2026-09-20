"""Declared statement extraction through rope, or explicit-parameter closure hoisting."""
from __future__ import annotations

import argparse
import ast
import symtable
import sys
from pathlib import Path

import libcst as cst
from common import (
    Refusal,
    definition,
    digest,
    module_name,
    publish,
    read_sources,
    resolve,
    terminal_newline,
)
from manifest_oracle import expected, selection
from move_methods import (
    add_imports,
    dependency_imports,
    format_imports,
    missing_imports,
    repair_source_imports,
)
from quality import config
from rope.base.project import Project
from rope.refactor.extract import ExtractMethod


def _inside_loop(node, chosen):
    """True when a break/continue in the range has its enclosing loop inside the range too."""
    for stmt in chosen:
        for loop in ast.walk(stmt):
            if isinstance(loop, (ast.For, ast.While, ast.AsyncFor)) and any(n is node for n in ast.walk(loop)):
                return True
    return False


def locate_cst(module, qualname):
    node = module
    for name in qualname.split('.'):
        body = node.body if isinstance(node, cst.Module) else node.body.body
        node = next(n for n in body if isinstance(n, (cst.FunctionDef, cst.ClassDef)) and n.name.value == name)
    return node


def hoist_cst(source, function, nested_name, name, free):
    module = cst.parse_module(source)
    outer = locate_cst(module, function)
    nested = locate_cst(module, function + '.' + nested_name)
    new = nested.with_changes(name=cst.Name(name), params=nested.params.with_changes(
        params=[*nested.params.params, *(cst.Param(cst.Name(p)) for p in free)]))

    class Rewrite(cst.CSTTransformer):
        def leave_FunctionDef(self, original, updated):
            if original is nested:
                return cst.RemoveFromParent()
            return updated

        def leave_Call(self, original, updated):
            if isinstance(original.func, cst.Name) and original.func.value == nested_name:
                return updated.with_changes(func=cst.Name(name), args=[*updated.args, *(cst.Arg(cst.Name(p)) for p in free)])
            return updated

    replaced = outer.visit(Rewrite())
    class ReplaceOuter(cst.CSTTransformer):
        def leave_FunctionDef(self, original, updated):
            return replaced if original is outer else updated
    return module.visit(ReplaceOuter()), new


def plan(root, source, function, name, start=None, end=None, dest=None, nested=None, import_root='.', max_parameters=None,
         retain_module_api=False, format_header=False):
    dest = dest or source
    before = read_sources(root, list(dict.fromkeys([source, dest])))
    if not name.isidentifier():
        raise Refusal('invalid extracted function name')
    trees = {p: ast.parse(s) for p, s in before.items()}
    if any((isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name) or
           (isinstance(n, ast.Name) and n.id == name) for tree in trees.values() for n in ast.walk(tree)):
        raise Refusal('extracted name already used')
    fn = definition(trees[source], function)
    if not isinstance(fn, ast.FunctionDef):
        raise Refusal('extract requires a synchronous function')
    limit = max_parameters or config(root)['max_parameters']
    op = dict(kind='hoist' if nested else 'extract', source=source, dest=dest, function=function, name=name, imports={})
    if nested:
        table = symtable.symtable(before[source], source, 'exec')
        for part in (function + '.' + nested).split('.'):
            table = next(c for c in table.get_children() if c.get_name() == part)
        free = sorted(table.get_frees())
        node = definition(fn, nested)
        if len(node.args.args) + len(free) > limit:
            raise Refusal('too many parameters; choose a more cohesive extract')
        op.update(nested=nested, free=free)
        # Independent oracle rejects escapes, nonlocals, recursion and complex signatures first.
        preliminary = dict(version=1, tier=2, baseline={p: digest(s) for p, s in before.items()}, operations=[op])
        expected(before, preliminary)
        modified, extracted = hoist_cst(before[source], function, nested, name, free)
    else:
        _block, chosen = selection(fn, start, end)
        if any(isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom, ast.Await, ast.Nonlocal, ast.Global,
                              ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for s in chosen for n in ast.walk(s)) \
                or any(isinstance(n, (ast.Break, ast.Continue)) and not _inside_loop(n, chosen) for s in chosen for n in ast.walk(s)):
            raise Refusal('range contains return/yield/await/global/nonlocal or nested definition; use --nested for closures')
        lines = before[source].splitlines(keepends=True)
        start_offset = sum(map(len, lines[:start - 1]))
        end_offset = sum(map(len, lines[:end]))
        project = Project(str(Path(root).resolve()), ropefolder=None)
        try:
            resource = project.get_file(source)
            changes = ExtractMethod(project, resource, start_offset, end_offset).get_changes(name, global_=True)
            if len(changes.changes) != 1 or changes.changes[0].resource.path != source:
                raise Refusal('rope changed files outside the declared source')
            rewritten = changes.changes[0].new_contents
        except Exception as exc:
            raise Refusal(f'rope refused extraction: {exc}') from exc
        finally:
            project.close()
        new = definition(ast.parse(rewritten), name)
        params = [a.arg for a in new.args.args]
        if len(params) > limit:
            raise Refusal('too many parameters; choose a more cohesive extract')
        returns = new.body[-1] if new.body else None
        outputs = []
        if isinstance(returns, ast.Return):
            value = returns.value
            names = value.elts if isinstance(value, ast.Tuple) else [value]
            if any(not isinstance(n, ast.Name) for n in names):
                raise Refusal('rope produced a non-name output')
            outputs = [n.id for n in names]
        op.update(start_line=start, end_line=end, parameters=params, outputs=outputs)
        modified = cst.parse_module(rewritten)
        extracted = locate_cst(modified, name)
        modified = modified.with_changes(body=[n for n in modified.body if n is not extracted])
    if dest == source:
        after = {source: modified.with_changes(body=[*modified.body, extracted]).code}
    else:
        target = cst.parse_module(before[dest])
        root_import = resolve(root, import_root)
        src_module = module_name(resolve(root, source).relative_to(root_import))
        dst_module = module_name(resolve(root, dest).relative_to(root_import))
        combined = modified.with_changes(body=[*modified.body, extracted]).code
        imported = dependency_imports(combined, [name], src_module, Path(source).name == '__init__.py')
        imported = missing_imports(before[dest], imported)
        op['imports'] = {source: missing_imports(before[source], [f'from {dst_module} import {name}'], 'source'),
                         dest: imported}
        modified = repair_source_imports(before[source], add_imports(modified, op['imports'][source]), op, source, retain_module_api)
        after = {source: modified.code,
                 dest: add_imports(target.with_changes(body=[*target.body, extracted]), imported).code}
    manifest = dict(version=1, tier=2, baseline={p: digest(s) for p, s in before.items()}, operations=[op])
    if format_header:
        format_imports(root, after, op)
    after = {path: terminal_newline(text) for path, text in after.items()}
    return before, after, manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', default='.')
    p.add_argument('--source', required=True)
    p.add_argument('--function', required=True)
    p.add_argument('--name', required=True)
    p.add_argument('--start', type=int)
    p.add_argument('--end', type=int)
    p.add_argument('--nested')
    p.add_argument('--dest')
    p.add_argument('--import-root', default='.')
    p.add_argument('--max-parameters', type=int)
    p.add_argument('--manifest', required=True)
    p.add_argument('--retain-module-api', action='store_true')
    p.add_argument('--format-imports', action='store_true')
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    try:
        if not a.nested and (a.start is None or a.end is None):
            raise Refusal('supply --start and --end, or --nested')
        if a.nested and (a.start is not None or a.end is not None):
            raise Refusal('range and closure operations must be separate')
        before, after, manifest = plan(a.project, a.source, a.function, a.name, a.start, a.end,
                                      a.dest, a.nested, a.import_root, a.max_parameters, a.retain_module_api, a.format_imports)
        publish(a.project, before, after, manifest, a.manifest, a.apply)
    except (Refusal, ValueError, KeyError, StopIteration) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
