"""Adversarial checks for the public CLIs, evidence replay and refusal boundaries."""
import ast
import itertools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import commit_all

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from collect_tests import collect, compare
from common import Refusal, publish
from extract_ranges import plan as extract_plan
from manifest_oracle import verify
from move_methods import plan

SNAPSHOT = SCRIPTS.parents[1] / 'split-module/scripts/snapshot_bodies.py'
def run(project, script, *arguments, ok=True):
    result = subprocess.run([sys.executable, str(script), *arguments], cwd=project,
                            capture_output=True, text=True, check=False)
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    return result


def test_snapshot_manifest_cli_and_legacy(project):
    run(project, SNAPSHOT, 'snapshot', 'sample', '--out', 'before.json')
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--strict')
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_calc.py', 'Engine', ['calculate'])
    publish(project, before, after, manifest, 'move.json', True)
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--manifest', 'move.json', '--strict')
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--strict', ok=False)
    path = project / 'sample/engine.py'
    path.write_text(path.read_text().replace('factor = 2', 'factor = 3'))
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--manifest', 'move.json', ok=False)


def test_method_only_tier3_waiver(project):
    run(project, SNAPSHOT, 'snapshot', 'sample', '--out', 'before.json')
    path = project / 'sample/engine.py'
    path.write_text(path.read_text().replace('sqrt(self.value) * self.factor', 'sqrt(self.value) * 3'))
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--strict', '--allow-changed', 'Engine.calculate', ok=False)
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--tier3', '--base', 'HEAD', '--allow-changed', 'Engine.calculate')
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--tier3', '--base', 'HEAD', '--allow-changed', 'Engine', ok=False)
    path.write_text(path.read_text().replace('factor = 2', 'factor = 4'))
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--tier3', '--base', 'HEAD', '--allow-changed', 'Engine.calculate', ok=False)


def test_oracle_rejects_added_removed_and_wrong_wrapper(project):
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_calc.py', 'Engine', ['build'])
    corruptions = [
        {**after, 'sample/_calc.py': after['sample/_calc.py'] + '\ndef invented(): return 2\n'},
        {**after, 'sample/_calc.py': ''},
        {**after, 'sample/engine.py': after['sample/engine.py'].replace('build = classmethod(', 'build = staticmethod(')},
        {**after, 'sample/invented.py': ''},
    ]
    for changed in corruptions:
        with pytest.raises(Refusal):
            verify(before, changed, manifest)
    wrong_tier = {**manifest, 'tier': 2}
    with pytest.raises(Refusal, match='tier'):
        verify(before, after, wrong_tier)


def test_dry_run_cli_writes_nothing(project):
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()}
    run(project, SCRIPTS / 'move_methods.py', '--source', 'sample/engine.py', '--class', 'Engine',
        '--methods', 'calculate', '--dest', 'sample/_calc.py', '--manifest', 'new/evidence.json')
    after = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()}
    assert before == after
    assert not (project / 'new').exists()


def test_stale_source_and_existing_manifest(project):
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_calc.py', 'Engine', ['calculate'])
    path = project / 'sample/engine.py'
    path.write_text(path.read_text() + '\n')
    with pytest.raises(Refusal, match='stale'):
        publish(project, before, after, manifest, 'move.json', True)
    path.write_text(before['sample/engine.py'])
    (project / 'move.json').write_text('existing evidence')
    with pytest.raises(Refusal, match='manifest output already exists'):
        publish(project, before, after, manifest, 'move.json', True)
    assert path.read_text() == before['sample/engine.py']
    assert not (project / 'sample/_calc.py').exists()
    assert (project / 'move.json').read_text() == 'existing evidence'


@pytest.mark.parametrize('source,reason', [
    ('class A:\n def run(self): return __class__\n', 'super'),
    ('class A:\n def run(self):\n  global x\n  x = 1\n', 'global'),
    ('class A:\n @staticmethod\n @property\n def run(self): return 1\n', 'stacked'),
    ('class A:\n @property\n def run(self): return 1\n @run.setter\n def run(self, value): pass\n', 'ambiguous|duplicate'),
    ('@decorator\nclass A:\n def run(self): return 1\n', 'decorated'),
    ('class A:\n def run(self, value=object()): return value\n', 'effectful'),
    ('len = lambda x: 42\nclass A:\n def run(self): return len([])\n', 'independent owner'),
    ('from math import sqrt\nsqrt = lambda x: 42\nclass A:\n def run(self): return sqrt(2)\n', 'independent owner'),
])
def test_additional_move_refusals(project, source, reason):
    (project / 'hazard.py').write_text(source)
    with pytest.raises(Refusal, match=reason):
        plan(project, 'hazard.py', 'helpers.py', 'A', ['run'])


@pytest.mark.parametrize('body,reason', [
    ('def nested(x):\n        return nested(x)\n    return nested(1)', 'recursive'),
    ('def nested(x):\n        return x\n    return nested', 'escapes'),
    ('def nested(x=1):\n        return x\n    return nested()', 'plain synchronous'),
    ('def nested(x):\n        return x\n    return nested(x=1)', 'positional'),
    ('def nested(x):\n        def inner(): return x\n        return inner()\n    return nested(1)', 'multiply nested'),
])
def test_hoist_refusals(project, body, reason):
    (project / 'closure.py').write_text('def outer():\n    ' + body + '\n')
    with pytest.raises(Refusal, match=reason):
        extract_plan(project, 'closure.py', 'outer', 'hoisted', nested='nested')


def test_extract_to_other_module_and_mutable_closure(project):
    source = 'def outer(values):\n    total = sum(values)\n    doubled = total * 2\n\n    return doubled\n'
    (project / 'range.py').write_text(source)
    commit_all(project)
    before, after, manifest = extract_plan(project, 'range.py', 'outer', 'calculate', 2, 3, dest='helpers.py')
    publish(project, before, after, manifest, 'extract.json', True)
    run(project, '-c', 'from range import outer; assert outer([1,2]) == 6')
    (project / 'closure.py').write_text('def outer(values):\n    def add(value):\n        values.append(value)\n    add(1)\n    return values\n')
    commit_all(project)
    before, after, manifest = extract_plan(project, 'closure.py', 'outer', 'add_value', nested='add')
    publish(project, before, after, manifest, 'hoist.json', True)
    run(project, '-c', 'from closure import outer; a=[]; assert outer(a) is a and a == [1]')


def test_pinned_test_mapping_and_duplicate_counts():
    baseline = {'ids': {'unittest': ['test.a', 'test.b']}, 'shards': {},
                'settings': {'pinned': {'unittest': ['test.a']}}}
    after = {**baseline, 'ids': {'unittest': ['test.c', 'test.b']}}
    with pytest.raises(Refusal, match='pinned'):
        compare(baseline, after, 'mapping', {'unittest': {'test.a': 'test.c'}})
    duplicate = {**baseline, 'ids': {'unittest': ['test.a', 'test.a']}}
    with pytest.raises(Refusal):
        compare(baseline, duplicate)


def test_custom_pytest_patterns_and_collection_failure(project):
    (project / 'pytest.ini').write_text('[pytest]\npython_classes = Spec\npython_files = spec_*.py\n')
    settings = dict(start='tests', top='.', pattern='test*.py', pytest=True, shards={})
    ids = collect(project, settings)
    (project / 'ids.json').write_text(json.dumps(ids))
    with pytest.raises(Refusal, match='pattern'):
        plan(project, 'tests/test_engine.py', 'tests/_support.py', 'TestEngine', ['test_summary'],
             'mixin', 'SpecMixin', True, test_snapshot='ids.json')
    (project / 'tests/test_broken.py').write_text('raise RuntimeError("collection failed")\n')
    with pytest.raises(Refusal, match='collection failed'):
        collect(project, settings)


def test_git_baseline_reconstruction_and_final_drift(project):
    run(project, SNAPSHOT, 'snapshot', 'sample', '--out', 'before.json')
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_calc.py', 'Engine', ['calculate'])
    publish(project, before, after, manifest, 'move.json', True)
    commit_all(project)
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--manifest', 'move.json')
    path = project / 'sample/_calc.py'
    path.write_text(path.read_text().replace('sqrt(self.value)', 'sqrt(self.value + 1)'))
    commit_all(project)
    run(project, SNAPSHOT, 'compare', 'before.json', 'sample', '--manifest', 'move.json', ok=False)


def test_package_relative_imports_future_and_repeat_destination(project):
    (project / 'sample/_values.py').write_text('VALUE = 2\n')
    (project / 'sample/__init__.py').write_text('from __future__ import annotations\nfrom ._values import VALUE\n'
                                              'class A:\n    def one(self): return VALUE\n    def two(self): return VALUE + 1\n')
    (project / 'sample/_helpers.py').write_text('import math\n')
    for method in ['one', 'two']:
        commit_all(project)
        before, after, manifest = plan(project, 'sample/__init__.py', 'sample/_helpers.py', 'A', [method])
        publish(project, before, after, manifest, f'{method}.json', True)
    run(project, '-c', 'from sample import A; assert A().one() == 2 and A().two() == 3')


@pytest.mark.skipif(not shutil.which('mypy'), reason='mypy not installed')
def test_function_binding_type_ratchet(project):
    def check():
        return subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', 'sample'], cwd=project,
                              capture_output=True, text=True, check=False)
    initial = check()
    assert initial.returncode == 0, initial.stdout
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_calc.py', 'Engine',
                                  ['calculate', 'add', 'build', 'doubled', 'temporary'], format_header=True)
    publish(project, before, after, manifest, 'move.json', True)
    final = check()
    assert final.returncode == 0, final.stdout
    lint = subprocess.run([sys.executable, '-m', 'ruff', 'check', '--isolated', 'sample'], cwd=project,
                          capture_output=True, text=True, check=False)
    assert lint.returncode == 0, lint.stdout


def test_quality_flags_giant_functions():
    from quality import DEFAULTS, report
    source = 'def giant(value):\n' + ''.join(f'    value += {n}\n' for n in range(160)) + '    return value\n'
    result = report(source, DEFAULTS)
    assert not result['debt']
    assert result['candidates'] == [{'kind': 'function', 'name': 'giant', 'line': 1, 'size': 162}]


def test_integrated_gate_and_mint_failure(project):
    run(project, SNAPSHOT, 'snapshot', 'sample', '--out', 'before.json')
    run(project, SCRIPTS / 'collect_tests.py', 'snapshot', '--out', 'ids.json')
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_calc.py', 'Engine',
                                  ['calculate', 'add', 'build', 'doubled', 'temporary'], format_header=True)
    publish(project, before, after, manifest, 'move.json', True)
    command = ['bash', str(SNAPSHOT.parent / 'verify.sh'), '--pkg', 'sample', '--snapshot', 'before.json',
               '--manifest', 'move.json', '--test-snapshot', 'ids.json', '--strict-bodies', '--mint']
    env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
           'REFACTOR_TEST_CMD': 'python3 -m unittest discover -s tests',
           'REFACTOR_MINT_CMD': 'true'}
    result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS bodies-unchanged' in result.stdout and 'PASS test-identities' in result.stdout
    env['REFACTOR_MINT_CMD'] = 'false'
    result = subprocess.run(command, cwd=project, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1 and 'FAIL mint' in result.stdout


def test_mixin_class_body_dependency_and_missing_identity_gate(project):
    path = project / 'tests/test_engine.py'
    path.write_text(path.read_text().replace('    def test_summary(self):',
                                           '    another_test = test_calculate\n\n    def test_summary(self):'))
    run(project, SCRIPTS / 'collect_tests.py', 'snapshot', '--out', 'ids.json')
    with pytest.raises(Refusal, match='class-body'):
        plan(project, 'tests/test_engine.py', 'tests/_support.py', 'TestEngine', ['test_calculate'],
             'mixin', 'CalcMixin', True, test_snapshot='ids.json')
    (project / 'manifest.json').write_text(json.dumps({'operations': [{'shape': 'mixin'}]}))
    result = subprocess.run(['bash', str(SNAPSHOT.parent / 'verify.sh'), '--pkg', 'sample',
                              '--snapshot', 'before.json', '--manifest', 'manifest.json'],
                             cwd=project, capture_output=True, text=True, check=False)
    assert result.returncode == 2 and '--test-snapshot' in result.stderr


def test_path_alias_and_non_python_destination(project):
    with pytest.raises(Refusal, match='alias'):
        plan(project, 'sample/engine.py', 'sample/./engine.py', 'Engine', ['calculate'])
    with pytest.raises(Refusal, match='Python'):
        plan(project, 'sample/engine.py', 'sample/helper.txt', 'Engine', ['calculate'])


def import_bindings(source):
    bindings = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bindings.extend(a.asname or a.name.split('.')[0] for a in node.names)
    return bindings


@pytest.mark.parametrize('options', list(itertools.product(list(itertools.product([False, True], repeat=2)), repeat=3)))
def test_shared_imports_across_three_move_clusters(project, options):
    source, dest = 'sample/shared.py', 'sample/_shared.py'
    (project / source).write_text('''from __future__ import annotations
import math as maths, json as jsonlib
from typing import Any, Mapping, Sequence, Callable, Iterable

class E:
    def a(self, value: Any, data: Mapping[str, Sequence[int]]) -> int:
        return maths.floor(value) + sum(data['items'])

    def b(self, value: Any, fn: Callable[[Any], int]) -> int:
        return maths.floor(fn(value)) + len(jsonlib.dumps([]))

    def c(self, values: Iterable[int], data: Mapping[str, int]) -> int:
        return sum(values) + data['extra']
''')
    for method, (format_header, annotate_self) in zip(['a', 'b', 'c'], options):
        commit_all(project)
        before, after, manifest = plan(project, source, dest, 'E', [method],
                                       format_header=format_header, annotate_self=annotate_self)
        assert verify(before, after, manifest)
        for path in [source, dest]:
            existing = set(import_bindings(before[path]))
            additions = import_bindings('\n'.join(manifest['operations'][0]['imports'][path]))
            assert not existing.intersection(additions)
            assert set(import_bindings(after[path])) == existing.union(additions) - set(
                manifest['operations'][0].get('remove_imports', {}).get(path, []))
            bindings = import_bindings(after[path])
            assert len(bindings) == len(set(bindings))
        publish(project, before, after, manifest, f'.refactor/shared-{method}.json', True)
    run(project, '-c', 'from sample.shared import E; e = E(); '
        'assert e.a(2.8, {"items": [3, 4]}) == 9; '
        'assert e.b(5, lambda x: x * 2) == 12; '
        'assert e.c([1, 2], {"extra": 4}) == 7')


@pytest.mark.parametrize('formats', list(itertools.product([False, True], repeat=3)))
def test_shared_imports_across_three_extract_clusters(project, formats):
    source, dest = 'sample/shared.py', 'sample/_shared.py'
    (project / source).write_text('''from __future__ import annotations
from typing import Any, Mapping, Sequence

def a():
    data: Any = 1
    return data

def b():
    data: Mapping[str, Any] = {'value': 2}
    return data['value']

def c():
    data: Sequence[Any] = [3]
    return data[0]
''')
    for name, format_header in zip(['a', 'b', 'c'], formats):
        commit_all(project)
        fn = next(n for n in ast.parse((project / source).read_text()).body if isinstance(n, ast.FunctionDef) and n.name == name)
        before, after, manifest = extract_plan(project, source, name, f'build_{name}',
                                               fn.body[0].lineno, fn.body[0].end_lineno,
                                               dest=dest, format_header=format_header)
        assert verify(before, after, manifest)
        for path in [source, dest]:
            additions = import_bindings('\n'.join(manifest['operations'][0]['imports'][path]))
            assert not set(import_bindings(before[path])).intersection(additions)
            bindings = import_bindings(after[path])
            assert len(bindings) == len(set(bindings))
        publish(project, before, after, manifest, f'.refactor/shared-{name}.json', True)
    run(project, '-c', 'from sample.shared import a, b, c; assert (a(), b(), c()) == (1, 2, 3)')


@pytest.mark.parametrize('operation', ['move', 'extract'])
@pytest.mark.parametrize('existing', [
    'from decimal import Decimal as Any\n',
    'from typing import Mapping as Any\n',
    'import math as Any\n',
    'def Any(): pass\n',
    'class Any: pass\n',
    'Any = 1\n',
    'from typing import Any\nAny = 1\n',
    'from typing import Any\nfrom decimal import Decimal as Any\n',
    'if True:\n    Any = 1\n',
])
def test_shared_imports_refuse_real_destination_clashes(project, operation, existing):
    source, dest = 'sample/shared.py', 'sample/_shared.py'
    text = 'from __future__ import annotations\nfrom typing import Any\n'
    if operation == 'move':
        text += 'class E:\n    def a(self):\n        data: Any = 1\n        return data\n'
    else:
        text += 'def a():\n    data: Any = 1\n    return data\n'
    (project / source).write_text(text)
    (project / dest).write_text(existing)
    with pytest.raises(Refusal, match='import name clash: Any'):
        if operation == 'move':
            plan(project, source, dest, 'E', ['a'])
        else:
            extract_plan(project, source, 'a', 'build_a', 4, 4, dest=dest)
    assert (project / source).read_text() == text and (project / dest).read_text() == existing


def test_shared_imports_respect_aliases_and_local_bindings(project):
    source, dest = 'sample/shared.py', 'sample/_shared.py'
    (project / source).write_text('''from . import _shared, engine
import math as maths
from typing import Any as Value, Mapping
kept = _shared.existing
class E:
    def a(self, data: Mapping[str, Value]) -> int:
        return maths.floor(data['value'])
''')
    (project / dest).write_text('''from typing import Sequence, Any as Value
import math as other_maths
def existing():
    Mapping = 1
    return Mapping
''')
    commit_all(project)
    before, after, manifest = plan(project, source, dest, 'E', ['a'])
    assert manifest['operations'][0]['imports'][dest] == ['import math as maths', 'from typing import Mapping']
    assert verify(before, after, manifest)
    publish(project, before, after, manifest, '.refactor/aliases.json', True)
    run(project, '-c', 'from sample.shared import E; assert E().a({"value": 2.8}) == 2')


def test_shared_imports_preserve_source_class_alias_refusal(project):
    (project / 'sample/shared.py').write_text('class E:\n    _shared = 1\n    def a(self): return 1\n')
    with pytest.raises(Refusal, match='source alias already bound: _shared'):
        plan(project, 'sample/shared.py', 'sample/_shared.py', 'E', ['a'])


@pytest.mark.parametrize('operation', ['move', 'extract'])
@pytest.mark.parametrize('changed', ['source', 'dest'])
def test_apply_cli_refuses_uncommitted_baseline_without_writes(project, operation, changed):
    source, dest = 'sample/engine.py', 'sample/_ops.py'
    (project / dest).write_text('"""Existing destination."""\n')
    commit_all(project)
    path = project / (source if changed == 'source' else dest)
    path.write_text(path.read_text() + '# Uncommitted change.\n')
    if operation == 'move':
        script = SCRIPTS / 'move_methods.py'
        arguments = ['--class', 'Engine', '--methods', 'calculate']
    else:
        script = SCRIPTS / 'extract_ranges.py'
        fn = next(n for n in ast.parse((project / source).read_text()).body
                  if isinstance(n, ast.FunctionDef) and n.name == 'summarize')
        arguments = ['--function', 'summarize', '--name', 'weighted_total',
                     '--start', str(fn.body[0].lineno), '--end', str(fn.body[1].end_lineno)]
    before = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()}
    result = run(project, script, '--source', source, '--dest', dest, *arguments,
                 '--manifest', '.refactor/uncommitted.json', '--apply', ok=False)
    assert 'commit the source/destination baseline before --apply' in result.stderr
    assert {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()} == before
    assert not (project / '.refactor').exists()


def test_manifest_compare_cli_refuses_snapshot_digest_mismatch_without_writes(project):
    (project / '.refactor').mkdir()
    snapshot_path, manifest_path = '.refactor/before.json', '.refactor/move.json'
    run(project, SNAPSHOT, 'snapshot', 'sample', '--out', snapshot_path)
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_ops.py', 'Engine', ['calculate'])
    publish(project, before, after, manifest, manifest_path, True)
    run(project, SNAPSHOT, 'compare', snapshot_path, 'sample', '--manifest', manifest_path)
    snapshot = json.loads((project / snapshot_path).read_text())
    # Keep the manifest and git baseline valid so this check cannot be masked by verify().
    snapshot['files']['sample/engine.py'] = '0' * 64
    (project / snapshot_path).write_text(json.dumps(snapshot))
    files = {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()}
    result = run(project, SNAPSHOT, 'compare', snapshot_path, 'sample', '--manifest', manifest_path, ok=False)
    assert 'manifest baseline differs from snapshot' in result.stderr
    assert {p.relative_to(project): p.read_bytes() for p in project.rglob('*') if p.is_file()} == files
