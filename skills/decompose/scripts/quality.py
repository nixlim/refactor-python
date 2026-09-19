"""One configuration and quality report for planners and guards."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path

DEFAULTS = dict(module_target=500, module_ceiling=500, function_target=150,
                class_target=30, max_parameters=6, plain_decorators=[])
# plain_decorators: project decorators known to be plain function wrappers (no descriptor
# semantics), written exactly as they appear in the class body, e.g. "_serialize_command"
# or "locks.serialize". A function-shape move re-applies them in the class binding.


def config(root='.', path=None):
    result = DEFAULTS.copy()
    filename = Path(path) if path else Path(root) / '.refactor-quality.json'
    if filename.exists():
        values = json.loads(filename.read_text())
        if set(values) - set(result):
            raise ValueError('unknown quality settings: ' + ', '.join(set(values) - set(result)))
        result.update(values)
    if 'REFACTOR_MAX_LINES' in os.environ:
        result['module_ceiling'] = int(os.environ['REFACTOR_MAX_LINES'])
    decorators = result['plain_decorators']
    if not isinstance(decorators, list) or any(not isinstance(d, str) or not d for d in decorators):
        raise ValueError('plain_decorators must be a list of decorator expressions')
    if any(type(v) is not int or v < 1 for k, v in result.items() if k != 'plain_decorators'):
        raise ValueError('quality settings must be positive integers')
    if result['module_target'] > result['module_ceiling']:
        raise ValueError('module_target must not exceed module_ceiling')
    return result


def report(source, limits):
    tree = ast.parse(source)
    lines = sum(bool(s.strip()) and not s.lstrip().startswith('#') for s in source.splitlines())
    candidates = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = node.end_lineno - node.lineno + 1
            if length > limits['function_target']:
                candidates.append(dict(kind='function', name=node.name, line=node.lineno, size=length))
        elif isinstance(node, ast.ClassDef):
            size = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in node.body)
            if size > limits['class_target']:
                candidates.append(dict(kind='class', name=node.name, line=node.lineno, size=size))
    return dict(code_lines=lines, over_ceiling=lines > limits['module_ceiling'],
                debt=lines > limits['module_target'], candidates=candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--config')
    parser.add_argument('--debt', help='JSON {path: {reason, follow_up}} for every over-target module')
    args = parser.parse_args()
    limits = config(path=args.config)
    debts = json.loads(Path(args.debt).read_text()) if args.debt else {}
    result = {p: report(Path(p).read_text(), limits) for p in args.paths}
    bad = False
    for path, item in result.items():
        item['tracking'] = debts.get(path)
        tracked = item['tracking'] or {}
        bad |= item['over_ceiling'] or bool(item['candidates']) or (
            item['debt'] and not (tracked.get('reason') and tracked.get('follow_up')))
    print(json.dumps(dict(limits=limits, modules=result), indent=2))
    return int(bad)


if __name__ == '__main__':
    raise SystemExit(main())
