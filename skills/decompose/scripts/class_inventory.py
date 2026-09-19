"""Method graph, conservative relocation verdicts, and repo-wide class-use census."""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

from common import FUNCTIONS, Refusal, definition, files, global_names

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'split-module' / 'scripts'))
from inventory import label_propagation, peel_hubs, top_level_symbols

KNOWN_DECORATORS = {'staticmethod', 'classmethod', 'property', 'contextmanager', 'contextlib.contextmanager'}


def method_facts(source, cls, method, plain_decorators=()):
    decorators = [ast.unparse(d) for d in method.decorator_list]
    nodes = list(ast.walk(method))
    attrs = [n for n in nodes if isinstance(n, ast.Attribute) and
             isinstance(n.value, ast.Name) and n.value.id in {'self', 'cls'}]
    calls = sorted({n.func.attr for n in nodes if isinstance(n, ast.Call) and
                    isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                    and n.func.value.id in {'self', 'cls'}})
    mangled = sorted({n.attr for n in nodes if isinstance(n, ast.Attribute) and
                      n.attr.startswith('__') and not n.attr.endswith('__')} |
                     {n.id for n in nodes if isinstance(n, ast.Name) and
                      n.id.startswith('__') and not n.id.endswith('__')})
    reasons = []
    if any(d not in KNOWN_DECORATORS and d not in plain_decorators for d in decorators):
        reasons.append('unknown decorator')
    if len(decorators) > 1:
        reasons.append('stacked decorators require explicit descriptor analysis')
    if any(isinstance(n, ast.Name) and n.id in {'super', '__class__'} for n in nodes):
        reasons.append('super/__class__ depends on defining class')
    if mangled or (method.name.startswith('__') and not method.name.endswith('__')):
        reasons.append('name mangling depends on defining class')
    if any(isinstance(n, ast.Global) for n in nodes):
        reasons.append('global writes require one owning module')
    if any(isinstance(n, ast.Name) and n.id in {'globals', 'locals', 'eval', 'exec'} for n in nodes):
        reasons.append('dynamic namespace access')
    class_names = {n.name for n in cls.body if isinstance(n, (*FUNCTIONS, ast.ClassDef))}
    class_names |= {n.id for s in cls.body if isinstance(s, (ast.Assign, ast.AnnAssign))
                    for n in ast.walk(s) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    header = [*method.args.defaults, *(x for x in method.args.kw_defaults if x), *method.decorator_list]
    header += [a.annotation for a in [*method.args.posonlyargs, *method.args.args,
                                     *method.args.kwonlyargs] if a.annotation]
    if method.returns:
        header.append(method.returns)
    if any(isinstance(n, ast.Name) and n.id in class_names | {cls.name}
           for h in header for n in ast.walk(h)):
        reasons.append('class-dependent default, annotation, or decorator')
    if any(isinstance(n, ast.Constant) and isinstance(n.value, str) and cls.name in n.value
           for h in header for n in ast.walk(h)):
        reasons.append('class-dependent forward annotation')
    if any(isinstance(n, (ast.Call, ast.NamedExpr, ast.Lambda))
           for value in [*method.args.defaults, *(x for x in method.args.kw_defaults if x)] for n in ast.walk(value)):
        reasons.append('effectful default evaluation changes execution order')
    return dict(name=method.name, line=method.lineno, end_line=method.end_lineno,
                decorators=decorators,
                reads=sorted({n.attr for n in attrs if isinstance(n.ctx, ast.Load)}),
                writes=sorted({n.attr for n in attrs if isinstance(n.ctx, (ast.Store, ast.Del))}),
                calls=calls, globals=sorted(global_names(source, f'{cls.name}.{method.name}')),
                nested_defs=[n.name for n in nodes if isinstance(n, FUNCTIONS) and n is not method],
                nonlocal_names=sorted({v for n in nodes if isinstance(n, ast.Nonlocal) for v in n.names}),
                mangled=mangled, reasons=reasons,
                verdict='unsupported' if reasons else ('wrap' if decorators else 'ok'))


def communities(methods, seeds=None, exclude_hubs=None, max_hubs=12):
    """Peel shared state/method hubs before clustering; don't make state cliques."""
    names = [m['name'] for m in methods]
    edges = set()
    for method in methods:
        edges.update((method['name'], call) for call in method['calls'] if call in names and call != method['name'])
        edges.update((method['name'], '@state:' + attr) for attr in set(method['reads'] + method['writes']) - set(names))
    nodes = names + sorted({b for _, b in edges if b.startswith('@state:')})
    _, hubs, core, _ = peel_hubs(nodes, sorted(edges), exclude_hubs, max_hubs)
    pinned = set()
    for members in (seeds or {}).values():
        for name in members:
            if name not in names or name in pinned:
                raise Refusal(f'unknown or multiply seeded method: {name}')
            pinned.add(name)
    # Seeded method hubs still need a plan assignment, but don't reconnect the graph.
    core += [n for n in hubs if n in pinned]
    groups = label_propagation(core, [(a, b) for a, b in edges if a in core and b in core and a not in hubs and b not in hubs], seeds=seeds)
    clusters = [[n for n in group if n in names] for group in groups]
    clusters += [[n] for n in hubs if n in names and n not in pinned]
    return sorted(edges), [c for c in clusters if c], hubs


def census(root, class_name):
    """Sites that name the class or a direct alias; no repository-wide patch dump.

    Dynamic lookup without a literal/alias is deliberately reported as unresolved
    by census_complete=False, rather than fabricating thousands of unrelated hits.
    """
    hits = []
    for path in files(root):
        try:
            source = path.read_text()
            if class_name not in source:
                continue
            tree = ast.parse(source)
        except (SyntaxError, UnicodeError) as exc:
            hits.append(dict(path=str(path), line=0, kind='unparsed', text=str(exc)))
            continue
        aliases = {class_name}
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                aliases |= {a.asname or a.name for a in n.names if a.name == class_name}
        def related(node):
            if isinstance(node, ast.Name):
                return node.id in aliases
            if isinstance(node, ast.Attribute):
                return node.attr == class_name or related(node.value)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                return bool(set(node.value.split('.')) & aliases)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'getattr':
                return len(node.args) >= 2 and (related(node.args[0]) or related(node.args[1]))
            return False
        for _ in range(3):
            for n in ast.walk(tree):
                if isinstance(n, ast.Assign) and related(n.value) and not isinstance(n.value, ast.Constant):
                    aliases |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        class Census(ast.NodeVisitor):
            def record(self, node, kind):
                hits.append(dict(path=str(path.relative_to(root)), line=node.lineno, kind=kind, text=ast.unparse(node)))

            def visit_ClassDef(self, node):
                if node.name != class_name:
                    self.generic_visit(node)

            def visit_Call(self, node):
                callee = ast.unparse(node.func)
                target = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == 'target'), None)
                if related(target) and ('patch' in callee or callee.rsplit('.', 1)[-1] in
                                                          {'setattr', 'delattr', 'vars', 'getattr', 'getsource'}):
                    self.record(node, 'patch-or-reflection')
                elif isinstance(node.func, ast.Attribute) and related(node.func.value):
                    self.record(node, 'unbound-call')
                else:
                    self.generic_visit(node)

            def visit_Attribute(self, node):
                if related(node.value):
                    kind = 'assignment' if isinstance(node.ctx, ast.Store) else (
                        'reflection' if node.attr in {'__dict__', '__qualname__'} else 'unbound-read')
                    self.record(node, kind)
                else:
                    self.generic_visit(node)
        Census().visit(tree)
    return hits


def inventory(source, class_name, root=None, seeds=None, exclude_hubs=None, max_hubs=12, plain_decorators=()):
    cls = definition(ast.parse(source), class_name)
    if not isinstance(cls, ast.ClassDef):
        raise Refusal('target is not a class')
    methods = [method_facts(source, cls, m, plain_decorators) for m in cls.body if isinstance(m, FUNCTIONS)]
    if len({m['name'] for m in methods}) != len(methods):
        raise Refusal('duplicate method definitions (including property setters/overloads)')
    edges, clusters, hubs = communities(methods, seeds, exclude_hubs, max_hubs)
    owners = {name: node for name, node, _ in top_level_symbols(ast.parse(source)) if name != class_name}
    prerequisites = [{'name': name, 'line': node.lineno,
                      'users': [{'method': f'{class_name}.{m["name"]}', 'line': m['line']} for m in methods if name in m['globals']],
                      'action': 'relocate to a non-discovered support module before method extraction'}
                     for name, node in owners.items() if any(name in m['globals'] for m in methods)]
    return dict(name=class_name, bases=[ast.unparse(b) for b in cls.bases],
                metaclass=[ast.unparse(k.value) for k in cls.keywords if k.arg == 'metaclass'],
                decorators=[ast.unparse(d) for d in cls.decorator_list],
                statements=[ast.unparse(n) for n in cls.body if not isinstance(n, FUNCTIONS)],
                slots=any(isinstance(n, ast.Name) and n.id == '__slots__' for n in ast.walk(cls)),
                methods=methods, edges=edges, clusters=clusters, hubs=hubs, prerequisites=prerequisites,
                census=census(Path(root), class_name) if root else [],
                census_complete=False)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source')
    p.add_argument('--class', dest='class_name', required=True)
    p.add_argument('--repo-root', default='.')
    p.add_argument('--seeds', help='JSON {seam: [method, ...]}')
    p.add_argument('--exclude-hubs', type=int)
    p.add_argument('--max-hubs', type=int, default=12)
    p.add_argument('--json', dest='output')
    a = p.parse_args()
    from quality import config
    plain = config(a.repo_root)['plain_decorators']
    result = inventory(Path(a.source).read_text(), a.class_name, a.repo_root,
                       json.loads(Path(a.seeds).read_text()) if a.seeds else None, a.exclude_hubs, a.max_hubs, plain)
    text = json.dumps(result, indent=2)
    if a.output:
        Path(a.output).write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
