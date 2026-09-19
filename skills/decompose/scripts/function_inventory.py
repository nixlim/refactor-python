"""Report statement ranges and conservative data-flow facts for extraction planning."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

from common import FUNCTIONS, definition


def flow(statements):
    reads, writes = set(), set()
    for stmt in statements:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name):
                (reads if isinstance(n.ctx, ast.Load) else writes).add(n.id)
            elif isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                reads.add(n.target.id)
    return reads, writes


def inventory(source, qualname):
    fn = definition(ast.parse(source), qualname)
    if not isinstance(fn, FUNCTIONS):
        raise ValueError('target is not a function')
    lines = source.splitlines()
    groups = []
    for stmt in fn.body:
        if not groups or any(not s.strip() or s.lstrip().startswith('#')
                             for s in lines[groups[-1][-1].end_lineno:stmt.lineno - 1]):
            groups.append([])
        groups[-1].append(stmt)
    blocks = []
    for group in groups:
        reads, writes = flow(group)
        later = [s for s in fn.body if s.lineno > group[-1].end_lineno]
        live, _ = flow(later)
        control = [{'kind': type(n).__name__, 'line': n.lineno} for s in group for n in ast.walk(s)
                   if isinstance(n, (ast.Return, ast.Yield, ast.YieldFrom, ast.Break, ast.Continue))]
        nested = []
        for s in group:
            for n in ast.walk(s):
                if isinstance(n, FUNCTIONS):
                    nr, nw = flow(n.body)
                    parameters = {a.arg for a in [*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs]}
                    nested.append(dict(name=n.name, line=n.lineno,
                                       free_candidates=sorted(nr - nw - parameters),
                                       nonlocal_names=[v for x in ast.walk(n) if isinstance(x, ast.Nonlocal) for v in x.names],
                                       mutable_closure_possible=any(isinstance(x, (ast.Call, ast.AugAssign, ast.Subscript))
                                                                   for x in ast.walk(n))))
        blocks.append(dict(start_line=group[0].lineno, end_line=group[-1].end_lineno,
                           input_candidates=sorted(reads), writes=sorted(writes),
                           output_candidates=sorted(writes & live), control=control, nested=nested))
    return dict(function=qualname, line=fn.lineno, end_line=fn.end_lineno, blocks=blocks,
                note='Conservative candidates; rope infers inputs/outputs. Control inside complete loops may be legal.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source')
    p.add_argument('--function', required=True)
    p.add_argument('--json', dest='output')
    a = p.parse_args()
    text = json.dumps(inventory(Path(a.source).read_text(), a.function), indent=2)
    if a.output:
        Path(a.output).write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()
