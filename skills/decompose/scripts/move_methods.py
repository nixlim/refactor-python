"""Move unchanged methods with LibCST; dry run unless --apply is supplied."""
from __future__ import annotations

import argparse
import ast
import subprocess
import symtable
import sys
from pathlib import Path

import libcst as cst
from class_inventory import inventory
from common import (
    Refusal,
    definition,
    digest,
    global_names,
    module_name,
    publish,
    read_sources,
    resolve,
    terminal_newline,
)


def dependency_tree(source):
    """Expand type references for analysis only; symtable then resolves their scopes."""
    tree = ast.parse(source)
    special = {'Literal': 'Literal', 'Annotated': 'Annotated'}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {'typing', 'typing_extensions'}:
            special.update({a.asname or a.name: a.name for a in node.names if a.name in special.values()})
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {'typing', 'typing_extensions'}:
                    special.update({f'{alias.asname or alias.name}.{name}': name for name in ('Literal', 'Annotated')})

    class ForwardReferences(ast.NodeTransformer):
        def visit_Constant(self, node):
            if isinstance(node.value, str):
                try:
                    return self.visit(ast.parse(node.value, mode='eval').body)
                except SyntaxError:
                    pass
            return node

        def visit_Subscript(self, node):
            kind = special.get(ast.unparse(node.value))
            if kind == 'Literal':
                return node  # Literal strings are values, not forward references.
            if kind == 'Annotated' and isinstance(node.slice, ast.Tuple):
                node.slice.elts[0] = self.visit(node.slice.elts[0])
                return node  # Metadata strings are values too.
            return self.generic_visit(node)

    class Annotations(ast.NodeTransformer):
        def visit_arg(self, node):
            if node.annotation:
                node.annotation = ForwardReferences().visit(node.annotation)
            return node

        def visit_AnnAssign(self, node):
            self.generic_visit(node)
            node.annotation = ForwardReferences().visit(node.annotation)
            return node

        def visit_FunctionDef(self, node):
            self.generic_visit(node)
            if node.returns:
                node.returns = ForwardReferences().visit(node.returns)
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

    Annotations().visit(tree)
    # Postponed annotations are otherwise omitted from Python's symbol table.
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == '__future__':
            node.names = [a for a in node.names if a.name != 'annotations']
    tree.body = [n for n in tree.body if not isinstance(n, ast.ImportFrom) or n.names]
    return tree


def dependency_imports(source, qualnames, source_module, source_is_package=False, include_decorators=True, extra_names=()):
    """Copy imports from their owner, never import back through the source facade.

    Globals still defined in source must be extracted first; no silent cycle repair.
    """
    tree = ast.parse(source)
    analysis = dependency_tree(source)
    analysis_source = ast.unparse(analysis)
    names = set(extra_names)
    uses = {}
    for q in qualnames:
        node = definition(tree, q)
        analysed = definition(analysis, q)
        headers = [analysed.args, *(analysed.decorator_list if include_decorators else [])]
        if analysed.returns:
            headers.append(analysed.returns)
        referenced = global_names(analysis_source, q) | {n.id for h in headers for n in ast.walk(h) if isinstance(n, ast.Name)}
        names |= referenced
        for name in referenced:
            uses.setdefault(name, []).append(f'{q} (line {node.lineno})')
    def owner_refusal(missing):
        details = '; '.join(f'{name}: {", ".join(uses.get(name, ["class header"]))}' for name in sorted(missing))
        raise Refusal('globals need an independent owner before moving: ' + details +
                      '. Relocate these constants/helpers to a non-discovered support module first, then import from that owner.')
    # Defaults/annotations contain builtins not reported by symtable.
    import builtins
    rebound = {n.id for statement in tree.body if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
               for n in ast.walk(statement) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
    module_bound = rebound | {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    module_bound |= {a.asname or a.name.split('.')[0] for n in tree.body
                     if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    names -= set(dir(builtins)) - module_bound
    if names & rebound:
        owner_refusal(names & rebound)
    result, found = [], set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == '__future__':
            result.append(ast.unparse(node))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            selected = [a for a in node.names if (a.asname or a.name.split('.')[0]) in names]
            if not selected:
                continue
            found |= {a.asname or a.name.split('.')[0] for a in selected}
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    package = source_module.split('.') if source_is_package else source_module.split('.')[:-1]
                    if node.level > len(package):
                        raise Refusal('relative import escapes import root')
                    module = '.'.join(package[:len(package) - node.level + 1] + ([node.module] if node.module else []))
                else:
                    module = node.module
                result.append(ast.unparse(ast.ImportFrom(module=module, names=selected, level=0)))
            else:
                result.append(ast.unparse(ast.Import(names=selected)))
    missing = names - found
    if missing:
        owner_refusal(missing)
    return result


def add_imports(module, statements):
    for text in statements:
        tree = ast.parse(module.code)
        node = ast.parse(text).body[0]
        if any(ast.dump(node, include_attributes=False) == ast.dump(n, include_attributes=False) for n in tree.body):
            continue
        future = isinstance(node, ast.ImportFrom) and node.module == '__future__'
        index = int(bool(tree.body) and isinstance(tree.body[0], ast.Expr) and
                    isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str))
        while index < len(tree.body) and isinstance(tree.body[index], (ast.Import, ast.ImportFrom)) and (
                not future or isinstance(tree.body[index], ast.ImportFrom) and tree.body[index].module == '__future__'):
            index += 1
        body = list(module.body)
        body.insert(index, cst.parse_statement(text + '\n'))
        module = module.with_changes(body=body)
    return module


def missing_imports(source, statements, location='destination'):
    """Reuse identical bindings, refusing other owners; declare only new aliases."""
    table = symtable.symtable(source, '<imports>', 'exec')
    assigned = {s.get_name() for s in table.get_symbols() if s.is_assigned()}
    bound = assigned | {s.get_name() for s in table.get_symbols() if s.is_imported()}
    origins = {}

    def binding(node, alias):
        if isinstance(node, ast.ImportFrom):
            return alias.asname or alias.name, ('from', node.level, node.module, alias.name)
        return alias.asname or alias.name.split('.')[0], ('import', alias.name)

    for node in ast.parse(source).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name, origin = binding(node, alias)
                origins.setdefault(name, set()).add(origin)
    added = []
    for text in statements:
        node = ast.parse(text).body[0]
        selected = []
        for alias in node.names:
            name, origin = binding(node, alias)
            if name in bound:
                if name in assigned or origins.get(name) != {origin}:
                    raise Refusal(f'{location} import name clash: {name}')
                continue
            selected.append(alias)
            bound.add(name)
            origins[name] = {origin}
        if selected:
            node.names = selected
            added.append(ast.unparse(node))
    return added


def repair_source_imports(original, updated, op, path, retain=False):
    """Drop newly unused imports; retaining implicit facade exports is opt-in."""
    old_used = {n.id for n in ast.walk(dependency_tree(original)) if isinstance(n, ast.Name)}
    new_used = {n.id for n in ast.walk(dependency_tree(updated.code)) if isinstance(n, ast.Name)}
    exported = {n.value for statement in ast.parse(updated.code).body if isinstance(statement, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == '__all__' for t in statement.targets)
                for n in ast.walk(statement.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    unused = old_used - new_used - exported
    removed = set()
    class Mark(cst.CSTTransformer):
        def visit_FunctionDef(self, node):
            return False

        def visit_ClassDef(self, node):
            return False

        def leave_SimpleStatementLine(self, original_node, updated_node):
            nodes = ast.parse(cst.Module(body=[updated_node]).code).body
            for node in nodes:
                if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                        (a.asname or a.name.split('.')[0]) in unused for a in node.names):
                    if not retain:
                        if len(nodes) != 1 or not isinstance(updated_node.body[0], (cst.Import, cst.ImportFrom)):
                            raise Refusal('normalize semicolon imports before moving')
                        aliases = [a for a, parsed in zip(updated_node.body[0].names, node.names)
                                   if (parsed.asname or parsed.name.split('.')[0]) not in unused]
                        removed.update((a.asname or a.name.split('.')[0]) for a in node.names
                                       if (a.asname or a.name.split('.')[0]) in unused)
                        if not aliases:
                            return cst.RemoveFromParent()
                        aliases[-1] = aliases[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
                        return updated_node.with_changes(body=[updated_node.body[0].with_changes(names=aliases)])
                    whitespace = updated_node.trailing_whitespace
                    comment = whitespace.comment.value if whitespace.comment else ''
                    if 'noqa' in comment:
                        continue
                    text = '# noqa: F401 - retained module API' + (f'; {comment.lstrip("# ")}' if comment else '')
                    return updated_node.with_changes(trailing_whitespace=whitespace.with_changes(
                        whitespace=cst.SimpleWhitespace('  '), comment=cst.Comment(text)))
            return updated_node
    result = updated.visit(Mark())
    if removed:
        op.setdefault('remove_imports', {})[path] = sorted(removed)
    return result


def format_imports(root, after, op):
    """Optional ordinary isort pass; the oracle permits only header regrouping."""
    op['format_imports'] = True
    for path, source in after.items():
        result = subprocess.run([sys.executable, '-m', 'ruff', 'check', '--select', 'I', '--fix', '--no-cache',
                                 '--stdin-filename', str(resolve(root, path)), '-'], cwd=root, input=source,
                                capture_output=True, text=True, check=False)
        if result.returncode:
            raise Refusal('import formatting failed: ' + result.stderr)
        after[path] = result.stdout
    return after


def add_type_checking_import(module, statement):
    block = cst.parse_statement(f'if TYPE_CHECKING:\n    {statement}\n')
    tree = ast.parse(module.code)
    wanted = ast.dump(ast.parse(cst.Module(body=[block]).code).body[0], include_attributes=False)
    existing = [n for n in tree.body if isinstance(n, ast.If) and
                isinstance(n.test, ast.Name) and n.test.id == 'TYPE_CHECKING']
    if existing:
        if len(existing) != 1 or ast.dump(existing[0], include_attributes=False) != wanted:
            raise Refusal('destination has a different TYPE_CHECKING block; merge by hand first')
        return module
    index = int(bool(tree.body) and isinstance(tree.body[0], ast.Expr) and
                isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str))
    while index < len(tree.body) and isinstance(tree.body[index], (ast.Import, ast.ImportFrom)):
        index += 1
    body = list(module.body)
    body.insert(index, block)
    return module.with_changes(body=body)


def plan(root, source, dest, class_name, methods, shape='function', target_class=None,
         test_only=False, import_root='.', test_snapshot=None, id_map=None, retain_module_api=False, format_header=False,
         annotate_self=False):
    if annotate_self and shape != 'function':
        raise Refusal('--annotate-self requires --shape function')
    if source == dest:
        raise Refusal('method moves require a distinct destination')
    before = read_sources(root, [source, dest])
    from quality import config
    info = inventory(before[source], class_name, plain_decorators=config(root)['plain_decorators'])
    cls = definition(ast.parse(before[source]), class_name)
    if cls.decorator_list or cls.keywords or info['slots']:
        raise Refusal('decorated classes, metaclasses, and __slots__ require design review')
    if shape != 'function' and not test_only:
        raise Refusal('mixin/class shapes are only supported for test classes')
    if shape != 'function':
        from collect_tests import validate_support, validate_test_scope
        validate_test_scope(root, source, class_name, test_snapshot)
        import json
        evidence = json.loads(resolve(root, test_snapshot).read_text())
        if import_root == '.':
            import_root = evidence['settings']['top']
        patterns = evidence.get('patterns', {})
        validate_support(dest, target_class, shape,
                         (evidence['settings']['pattern'], *patterns.get('files', ['test_*.py', '*_test.py'])),
                         patterns.get('classes', ['Test*']))
    if not methods or len(set(methods)) != len(methods):
        raise Refusal('empty or duplicate methods')
    facts = {m['name']: m for m in info['methods']}
    for name in methods:
        if name not in facts:
            raise Refusal(f'unknown method: {name}')
        if facts[name]['verdict'] == 'unsupported':
            raise Refusal(f'{class_name}.{name} (line {facts[name]["line"]}): ' + '; '.join(facts[name]['reasons']))
    if shape == 'mixin':
        for member in cls.body:
            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if member.name in methods:
                    continue
                scope = [member.args, *member.decorator_list, *([member.returns] if member.returns else [])]
            else:
                scope = [member]
            if any(isinstance(n, ast.Name) and n.id in methods for item in scope for n in ast.walk(item)):
                raise Refusal('class-body expression depends on a method removed into the mixin')
    if shape == 'class':
        nondefs = [s for s in cls.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))]
        if nondefs:
            raise Refusal('sibling class extraction requires class state to live in a shared support base')
        if any(set(facts[n]['calls']) & (set(facts) - set(methods)) for n in methods):
            raise Refusal('sibling class would lose calls to remaining methods')
    destination = ast.parse(before[dest])
    bound = {n.id for n in ast.walk(destination) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    bound |= {n.name for n in destination.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    bound |= {a.asname or a.name.split('.')[0] for n in destination.body
              if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    new_names = set(methods) if shape == 'function' else {target_class}
    if None in new_names or any(not n.isidentifier() for n in new_names) or bound & new_names:
        raise Refusal('missing/invalid destination name or name clash')
    import_base = resolve(root, import_root)
    src_module = module_name(resolve(root, source).relative_to(import_base))
    dst_module = module_name(resolve(root, dest).relative_to(import_base))
    alias = Path(dest).stem
    sibling = Path(source).parent == Path(dest).parent and '.' in dst_module
    module_import = f'from . import {alias}' if sibling else (
        f'from {dst_module.rsplit(".", 1)[0]} import {alias}' if '.' in dst_module else f'import {alias}')
    if target_class and any(isinstance(n, ast.Name) and n.id == target_class or
                            isinstance(n, ast.ClassDef) and n.name == target_class for n in ast.walk(cls)):
        raise Refusal('target class clashes with source')
    imports = dependency_imports(before[source], [f'{class_name}.{n}' for n in methods], src_module,
                                 Path(source).name == '__init__.py', include_decorators=shape != 'function',
                                 extra_names={n.id for base in cls.bases for n in ast.walk(base)
                                              if isinstance(n, ast.Name)} if shape == 'class' else ())
    if annotate_self:
        imports.append('from typing import TYPE_CHECKING')
    source_imports = [module_import] if shape == 'function' else (
        [f'from {dst_module} import {target_class}'] if shape == 'mixin' else [])
    imports = missing_imports(before[dest], imports)
    source_imports = missing_imports(before[source], source_imports, 'source')
    if shape == 'function' and source_imports and any(
            isinstance(n, ast.Name) and n.id == alias for n in ast.walk(ast.parse(before[source]))):
        raise Refusal(f'source alias already bound: {alias}')
    op = dict(kind='move', source=source, dest=dest, **{'class': class_name}, methods=methods,
              shape=shape, target_class=target_class, alias=alias, test_only=test_only,
              imports={source: source_imports, dest: imports})
    if annotate_self:
        op.update(annotate_self=True, type_checking_imports={dest: [f'from {src_module} import {class_name}']})
    manifest = dict(version=1, tier=1, baseline={p: digest(s) for p, s in before.items()}, operations=[op])
    if id_map:
        manifest['test_id_map'] = id_map
    module = cst.parse_module(before[source])
    source_class = next((n for n in module.body if isinstance(n, cst.ClassDef) and n.name.value == class_name), None)
    if source_class is None or not isinstance(source_class.body, cst.IndentedBlock):
        raise Refusal('class must be a top-level indented definition')
    moved = []
    retained = []
    for stmt in source_class.body.body:
        if isinstance(stmt, cst.FunctionDef) and stmt.name.value in methods:
            fn = stmt.with_changes(decorators=[]) if shape == 'function' else stmt
            if annotate_self and 'staticmethod' not in facts[stmt.name.value]['decorators']:
                field = 'posonly_params' if fn.params.posonly_params else 'params'
                params = list(getattr(fn.params, field))
                if params and params[0].annotation is None:
                    annotation = (f'type[{class_name}]' if 'classmethod' in facts[stmt.name.value]['decorators']
                                  else class_name)
                    params[0] = params[0].with_changes(annotation=cst.Annotation(cst.SimpleString(f'"{annotation}"')))
                    fn = fn.with_changes(params=fn.params.with_changes(**{field: params}))
            moved.append(fn)
            if shape == 'function':
                # Construct independently from the oracle's AST binding implementation.
                expr = cst.Attribute(cst.Name(alias), cst.Name(stmt.name.value))
                for decorator in reversed(stmt.decorators):
                    expr = cst.Call(decorator.decorator, [cst.Arg(expr)])
                line = cst.SimpleStatementLine([cst.Assign([cst.AssignTarget(stmt.name)], expr)])
                retained.append(line)
        else:
            retained.append(stmt)
    # Destination order is the explicitly requested order.
    moved.sort(key=lambda n: methods.index(n.name.value))
    bases = list(source_class.bases)
    if shape == 'mixin':
        bases.insert(0, cst.Arg(cst.Name(target_class)))
    updated_class = source_class.with_changes(bases=bases, body=source_class.body.with_changes(
        body=retained or [cst.parse_statement('pass\n')]))
    updated = module.with_changes(body=[updated_class if n is source_class else n for n in module.body])
    target = cst.parse_module(before[dest])
    if shape != 'function':
        moved = [cst.ClassDef(name=cst.Name(target_class),
                             bases=source_class.bases if shape == 'class' else [],
                             body=cst.IndentedBlock(body=moved))]
    target = target.with_changes(body=[*target.body, *moved])
    updated = repair_source_imports(before[source], add_imports(updated, source_imports), op, source, retain_module_api)
    target = add_imports(target, imports)
    if annotate_self:
        target = add_type_checking_import(target, op['type_checking_imports'][dest][0])
    after = {source: updated.code,
             dest: target.code}
    if format_header:
        format_imports(root, after, op)
    after = {path: terminal_newline(text) for path, text in after.items()}
    return before, after, manifest


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', default='.')
    p.add_argument('--source', required=True)
    p.add_argument('--dest', required=True)
    p.add_argument('--class', dest='class_name', required=True)
    p.add_argument('--methods', required=True)
    p.add_argument('--shape', choices=['function', 'mixin', 'class'], default='function')
    p.add_argument('--target-class')
    p.add_argument('--test-only', action='store_true')
    p.add_argument('--test-snapshot')
    p.add_argument('--id-map', help='JSON mapping by collector; required when applying class shape')
    p.add_argument('--import-root', default='.')
    p.add_argument('--manifest', required=True)
    p.add_argument('--retain-module-api', action='store_true', help='retain newly unused imports only for facade modules')
    p.add_argument('--format-imports', action='store_true', help='optional isort pass, verified by the oracle')
    p.add_argument('--annotate-self', action='store_true', help='keep function-shape receiver types via TYPE_CHECKING')
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    try:
        import json
        if a.shape == 'class' and a.apply and not a.id_map:
            raise Refusal('applying class shape requires --id-map')
        before, after, manifest = plan(a.project, a.source, a.dest, a.class_name, a.methods.split(','),
                                      a.shape, a.target_class, a.test_only, a.import_root, a.test_snapshot,
                                      json.loads(resolve(a.project, a.id_map).read_text()) if a.id_map else None,
                                      a.retain_module_api, a.format_imports, a.annotate_self)
        publish(a.project, before, after, manifest, a.manifest, a.apply)
    except (Refusal, ValueError, KeyError) as e:
        print(f'REFUSED: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
