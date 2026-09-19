"""Independent AST reconstruction of declared moves and statement extractions.

The manifest is an explicit structural contract, not a proof of Python equivalence.
Whole files are compared, including imports, class headers, decorators and docstrings.
"""
from __future__ import annotations

import ast
import copy

from common import FUNCTIONS, Refusal, definition, digest, dump


def verify_scope(snapshot, current, touched):
    """Compact file digests supplement (not replace) the legacy body snapshot."""
    if 'files' not in snapshot:
        raise Refusal('take a fresh hash-only snapshot for decompose scope checking')
    if not touched <= current.keys():
        raise Refusal('comparison paths omit a manifest output')
    if set(current) - touched != set(snapshot['files']) - touched:
        raise Refusal('unmanifested file added or removed')
    for path in set(current) - touched:
        if digest(current[path]) != snapshot['files'][path]:
            raise Refusal(f'unmanifested change: {path}')


def verify_allowed_methods(before, after, allowed):
    """Explicit decompose tier-3 check; legacy --allow-changed stays independent."""
    allowed = {n.strip() for n in allowed if n.strip()}
    if not allowed or any('.' not in n for n in allowed):
        raise Refusal('tier 3 allow-list must name methods, never a bare class')
    if set(before) != set(after):
        raise Refusal('tier 3 body waiver cannot relocate files')
    matched = set()
    for path in before:
        old, new = ast.parse(before[path]), ast.parse(after[path])
        for name in allowed:
            try:
                method = definition(old, name)
            except Refusal:
                continue
            other = definition(new, name)
            if not isinstance(definition(old, name.rsplit('.', 1)[0]), ast.ClassDef) or not isinstance(method, FUNCTIONS):
                raise Refusal('allow-list entries must be methods')
            if not isinstance(other, type(method)) or name in matched:
                raise Refusal('changed or ambiguous method identity')
            method.body = other.body = [ast.Pass()]
            matched.add(name)
        if dump(old) != dump(new):
            raise Refusal(f'change outside allowed method bodies: {path}')
    if matched != allowed:
        raise Refusal('unknown allow-list method')


def header_range(tree):
    start = int(bool(tree.body) and isinstance(tree.body[0], ast.Expr) and
                isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str))
    end = start
    while end < len(tree.body) and isinstance(tree.body[end], (ast.Import, ast.ImportFrom)):
        end += 1
    return start, end


def normalise_header(tree):
    """Permit only import ordering/grouping when the plan opted into isort."""
    start, end = header_range(tree)
    flat = []
    for node in tree.body[start:end]:
        for alias in node.names:
            item = copy.deepcopy(node)
            item.names = [alias]
            flat.append(item)
    tree.body[start:end] = sorted(flat, key=dump)


def remove_imports(tree, names):
    referenced = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    if set(names) & referenced:
        raise Refusal('cannot remove an import still referenced by the source')
    found = set()
    body = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.update(a.asname or a.name.split('.')[0] for a in node.names
                         if (a.asname or a.name.split('.')[0]) in names)
            node.names = [a for a in node.names if (a.asname or a.name.split('.')[0]) not in names]
            if not node.names:
                continue
        body.append(node)
    if found != set(names):
        raise Refusal('declared import removal does not match baseline')
    tree.body = body


def imports(tree, statements):
    for text in statements:
        nodes = ast.parse(text).body
        if len(nodes) != 1 or not isinstance(nodes[0], (ast.Import, ast.ImportFrom)):
            raise Refusal('manifest import is not a single import statement')
        if any(dump(n) == dump(nodes[0]) for n in tree.body):
            continue
        index = int(bool(tree.body) and isinstance(tree.body[0], ast.Expr) and
                    isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str))
        future = isinstance(nodes[0], ast.ImportFrom) and nodes[0].module == '__future__'
        while index < len(tree.body) and isinstance(tree.body[index], (ast.Import, ast.ImportFrom)) and (
                not future or isinstance(tree.body[index], ast.ImportFrom) and tree.body[index].module == '__future__'):
            index += 1
        tree.body.insert(index, nodes[0])


def binding(method, alias):
    expr = ast.Attribute(value=ast.Name(id=alias, ctx=ast.Load()), attr=method.name, ctx=ast.Load())
    # Decorators are applied in original class scope, in original order.
    for decorator in reversed(method.decorator_list):
        expr = ast.Call(func=copy.deepcopy(decorator), args=[expr], keywords=[])
    return ast.Assign(targets=[ast.Name(id=method.name, ctx=ast.Store())], value=expr)


def move(trees, op):
    src, dst = trees[op['source']], trees[op['dest']]
    cls = definition(src, op['class'])
    methods = [definition(cls, name) for name in op['methods']]
    if not methods or len(set(op['methods'])) != len(methods):
        raise Refusal('empty or duplicate method selection')
    if any(not isinstance(n, FUNCTIONS) for n in methods):
        raise Refusal('selected member is not a method')
    if op['shape'] == 'function':
        for method in methods:
            cls.body[cls.body.index(method)] = binding(method, op['alias'])
            fn = copy.deepcopy(method)
            fn.decorator_list = []
            dst.body.append(fn)
    elif op['shape'] in {'mixin', 'class'}:
        if not op.get('test_only'):
            raise Refusal('mixin/class shapes require test_only')
        new = ast.ClassDef(name=op['target_class'], bases=copy.deepcopy(cls.bases) if op['shape'] == 'class' else [],
                           keywords=copy.deepcopy(cls.keywords) if op['shape'] == 'class' else [],
                           body=copy.deepcopy(methods), decorator_list=[])
        # On Python 3.12+ this field is part of the AST contract.
        if hasattr(cls, 'type_params'):
            new.type_params = []
        dst.body.append(new)
        cls.body = [n for n in cls.body if n not in methods] or [ast.Pass()]
        if op['shape'] == 'mixin':
            cls.bases.insert(0, ast.Name(id=op['target_class'], ctx=ast.Load()))
    else:
        raise Refusal('unknown move shape')


def statement_lists(fn):
    """Every statement list inside the function (body, else, except, finally), outermost first."""
    pending = [fn.body]
    while pending:
        block = pending.pop(0)
        yield block
        for stmt in block:
            for field in ('body', 'orelse', 'finalbody'):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    pending.append(inner)
            for handler in getattr(stmt, 'handlers', []):
                pending.append(handler.body)
            for case in getattr(stmt, 'cases', []):
                pending.append(case.body)


def selection(fn, start, end):
    """The complete, consecutive statements of one statement list that span exactly start..end.

    The list may be nested inside a compound statement of the function; a range that
    cuts through a statement or straddles two lists is refused.
    """
    for block in statement_lists(fn):
        chosen = [s for s in block if s.lineno >= start and s.end_lineno <= end]
        if chosen and chosen[0].lineno == start and chosen[-1].end_lineno == end:
            return block, chosen
    raise Refusal('range must span complete consecutive statements of one block in the selected function')


def result_value(names, context=ast.Load):
    if len(names) == 1:
        return ast.Name(id=names[0], ctx=context())
    return ast.Tuple(elts=[ast.Name(id=n, ctx=context()) for n in names], ctx=context())


def extract(trees, op):
    src, dst = trees[op['source']], trees[op['dest']]
    fn = definition(src, op['function'])
    block, chosen = selection(fn, op['start_line'], op['end_line'])
    if any(isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom, ast.Await, ast.Nonlocal, ast.Global))
           for stmt in chosen for n in ast.walk(stmt)):
        raise Refusal('return/yield/await/nonlocal/global in extracted range')
    loops = [n for stmt in chosen for n in ast.walk(stmt) if isinstance(n, (ast.For, ast.AsyncFor, ast.While))]
    for stmt in chosen:
        for n in ast.walk(stmt):
            if isinstance(n, (ast.Break, ast.Continue)) and not any(any(x is n for x in ast.walk(l)) for l in loops):
                raise Refusal('break/continue in extracted range leaves the range')
    params, outputs = op['parameters'], op['outputs']
    if len(set(params)) != len(params) or len(set(outputs)) != len(outputs):
        raise Refusal('duplicate extraction parameters/outputs')
    new = ast.parse(f'def {op["name"]}({", ".join(params)}):\n    pass\n').body[0]
    new.body = copy.deepcopy(chosen)
    if outputs:
        new.body.append(ast.Return(value=result_value(outputs)))
    call = ast.Call(func=ast.Name(id=op['name'], ctx=ast.Load()),
                    args=[ast.Name(id=p, ctx=ast.Load()) for p in params], keywords=[])
    replacement = ast.Assign(targets=[result_value(outputs, ast.Store)], value=call) if outputs else ast.Expr(value=call)
    index = block.index(chosen[0])
    block[index:index + len(chosen)] = [replacement]
    dst.body.append(new)


def hoist(trees, op):
    src, dst = trees[op['source']], trees[op['dest']]
    outer = definition(src, op['function'])
    nested = definition(outer, op['nested'])
    if not isinstance(nested, ast.FunctionDef) or nested.decorator_list or nested.args.defaults or \
            nested.args.kw_defaults or nested.args.posonlyargs or nested.args.kwonlyargs or \
            nested.args.vararg or nested.args.kwarg or nested.returns or any(a.annotation for a in nested.args.args):
        raise Refusal('hoist requires a plain synchronous nested function with simple parameters')
    if any(isinstance(n, (ast.Nonlocal, ast.Global, ast.Yield, ast.YieldFrom)) for n in ast.walk(nested)):
        raise Refusal('nonlocal/global/generator closure cannot be hoisted')
    if any(isinstance(n, FUNCTIONS) and n is not nested for n in ast.walk(nested)):
        raise Refusal('multiply nested closure')
    if any(isinstance(n, ast.Name) and n.id == nested.name for n in ast.walk(nested)):
        raise Refusal('recursive nested function')
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
              ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
    for stmt in outer.body:
        if stmt is nested:
            continue
        if any(isinstance(n, scopes) and any(isinstance(x, ast.Name) and x.id == nested.name for x in ast.walk(n))
               for n in ast.walk(stmt)):
            raise Refusal('nested function used inside another lexical scope')
    new = copy.deepcopy(nested)
    new.name = op['name']
    new.args.args += [ast.arg(arg=n) for n in op['free']]
    outer.body.remove(nested)

    class Calls(ast.NodeTransformer):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == nested.name:
                if any(isinstance(a, ast.Starred) for a in node.args) or node.keywords:
                    raise Refusal('hoisted calls must use positional arguments')
                node.func.id = op['name']
                node.args += [ast.Name(id=n, ctx=ast.Load()) for n in op['free']]
                # Inspect arguments too, without treating this call target as an escape.
                node.args = [self.visit(a) for a in node.args]
                return node
            return self.generic_visit(node)

        def visit_Name(self, node):
            if node.id == nested.name:
                raise Refusal('nested function escapes or is rebound')
            return node

    Calls().visit(outer)
    dst.body.append(new)


def expected(before, manifest):
    if manifest.get('version') != 1 or manifest.get('tier') not in (1, 2):
        raise Refusal('unsupported manifest version/tier')
    if set(before) != set(manifest['baseline']):
        raise Refusal('manifest baseline file set differs')
    for path, source in before.items():
        if digest(source) != manifest['baseline'][path]:
            raise Refusal(f'manifest baseline mismatch: {path}')
    trees = {p: ast.parse(s) for p, s in before.items()}
    operations = manifest['operations']
    if len(operations) != 1:
        raise Refusal('one operation per cluster manifest; chain snapshots for sequential operations')
    op = operations[0]
    if (op['kind'] == 'move') != (manifest['tier'] == 1):
        raise Refusal('operation does not belong to declared tier')
    handlers = {'move': move, 'extract': extract, 'hoist': hoist}
    if op['kind'] not in handlers:
        raise Refusal('unknown operation')
    handlers[op['kind']](trees, op)
    for path, statements in op.get('imports', {}).items():
        imports(trees[path], statements)
    for path, names in op.get('remove_imports', {}).items():
        remove_imports(trees[path], names)
    return trees


def verify(before, after, manifest):
    trees = expected(before, manifest)
    if set(after) != set(trees):
        raise Refusal('unexpected/missing output file')
    for path, tree in trees.items():
        actual = ast.parse(after[path])
        if manifest['operations'][0].get('format_imports'):
            normalise_header(tree)
            normalise_header(actual)
        if dump(tree) != dump(actual):
            raise Refusal(f'undeclared structural change: {path}')
    return True
