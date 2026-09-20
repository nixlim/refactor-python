"""Regression and direct oracle tests; no mover precondition masks these guards."""
import ast
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import commit_all

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
SPLIT = SCRIPTS.parents[1] / 'split-module' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from class_inventory import census, inventory
from collect_tests import collect, compare
from common import Refusal, digest, publish
from extract_ranges import plan as extract_plan
from manifest_oracle import expected, header_range, verify, verify_allowed_methods, verify_scope
from move_methods import dependency_imports, plan


def test_oracle_itself_refuses_return_extraction():
    before = {'a.py': 'def outer(value):\n    return value\n'}
    after = {'a.py': 'def outer(value):\n    double(value)\ndef double(value):\n    return value\n'}
    manifest = {'version': 1, 'tier': 2, 'baseline': {p: digest(s) for p, s in before.items()},
                'operations': [{'kind': 'extract', 'source': 'a.py', 'dest': 'a.py', 'function': 'outer',
                                'start_line': 2, 'end_line': 2, 'name': 'double', 'parameters': ['value'], 'outputs': []}]}
    with pytest.raises(Refusal, match='return/yield'):
        verify(before, after, manifest)


@pytest.mark.parametrize('change', ['added', 'removed', 'edited'])
def test_oracle_itself_checks_untouched_scope(change):
    snapshot = {'files': {'source.py': digest('before'), 'untouched.py': digest('class A: pass\n')}}
    current = {'source.py': 'after', 'dest.py': 'moved', 'untouched.py': 'class A: pass\n'}
    if change == 'added':
        current['extra.py'] = ''
    elif change == 'removed':
        del current['untouched.py']
    else:
        current['untouched.py'] = 'class A: value = 2\n'
    with pytest.raises(Refusal, match='unmanifested'):
        verify_scope(snapshot, current, {'source.py', 'dest.py'})


def test_tier3_itself_refuses_bare_class():
    sources = {'a.py': 'class Engine:\n    def run(self): return 1\n'}
    with pytest.raises(Refusal, match='never a bare class'):
        verify_allowed_methods(sources, sources, ['Engine'])


@pytest.mark.parametrize('preamble,name,line', [('VALUE = 1', 'VALUE', 3), ('def helper(): return 1', 'helper', 3)])
def test_import_owner_refusal_names_method_and_line(preamble, name, line):
    source = f'{preamble}\nclass Engine:\n    def run(self): return {name}{"()" if name == "helper" else ""}\n'
    with pytest.raises(Refusal, match=rf'{name}: Engine.run \(line {line}\)'):
        dependency_imports(source, ['Engine.run'], 'pkg.engine')


def test_collector_itself_refuses_discovery_pattern_change():
    before = {'ids': {'unittest': ['a.test']}, 'shards': {}, 'settings': {}, 'patterns': {'classes': ['Test']}}
    after = {**before, 'patterns': {'classes': ['Spec']}}
    with pytest.raises(Refusal, match='discovery patterns changed'):
        compare(before, after)


def test_rope_other_file_refusal_is_not_masked(project, monkeypatch):
    import extract_ranges
    source = 'def outer(value):\n    result = value * 2\n    return result\n'
    (project / 'range.py').write_text(source)
    rewritten = 'def outer(value):\n    result = double(value)\n    return result\n\ndef double(value):\n    result = value * 2\n    return result\n'
    changes = SimpleNamespace(changes=[SimpleNamespace(resource=SimpleNamespace(path='other.py'), new_contents=rewritten)])
    monkeypatch.setattr(extract_ranges.ExtractMethod, 'get_changes', lambda *a, **kw: changes)
    with pytest.raises(Refusal, match='rope changed files outside'):
        extract_plan(project, 'range.py', 'outer', 'double', 2, 2)


def test_split_module_v1_top_level_allow_changed(project):
    (project / 'functions.py').write_text('def a(): return 1\ndef b(): return 2\n')
    subprocess.run([sys.executable, str(SPLIT / 'snapshot_bodies.py'), 'snapshot', 'functions.py', '--out', 'before.json'],
                   cwd=project, capture_output=True, check=True)
    snapshot = json.loads((project / 'before.json').read_text())
    assert 'sources' not in snapshot
    # Original consumers have exactly these two keys.
    (project / 'before.json').write_text(json.dumps({k: snapshot[k] for k in ('hashes', 'where')}))
    (project / 'functions.py').write_text('def a(): return 3\ndef b(): return 4\n')
    result = subprocess.run([sys.executable, str(SPLIT / 'snapshot_bodies.py'), 'compare', 'before.json',
                             'functions.py', '--strict', '--allow-changed', 'a,b'], cwd=project,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_file_hook_is_standalone_and_ignores_bad_decompose_config(tmp_path):
    shutil.copy(SPLIT / 'check_file_length.py', tmp_path / 'guard.py')
    (tmp_path / '.refactor-quality.json').write_text('not json')
    (tmp_path / 'long.py').write_text('value = 1\n' * 501)
    result = subprocess.run([sys.executable, 'guard.py', '--hook'], cwd=tmp_path,
                            input=json.dumps({'tool_input': {'file_path': 'long.py'}}),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 2
    assert 'FILE SIZE GUARD' in result.stderr and 'Traceback' not in result.stderr


def test_namespace_collector_default_and_preset(project):
    (project / 'tests/__init__.py').unlink()
    settings = dict(start='tests', pattern='test*.py', pytest=False, shards={})
    automatic = collect(project, settings)
    explicit = collect(project, {**settings, 'preset': 'namespace'})
    assert automatic['settings']['top'] == 'tests'
    assert automatic['ids'] == explicit['ids']
    assert all(i.startswith('test_engine.TestEngine.') for i in automatic['ids']['unittest'])


@pytest.mark.parametrize('namespace', [False, True])
def test_preamble_relocation_then_mixin_preserves_collection_and_behavior(project, namespace):
    if namespace:
        (project / 'tests/__init__.py').unlink()
    (project / 'tests/test_preamble.py').write_text('''import unittest

VALUE = 21

def helper(value):
    return value * 2

class TestPreamble(unittest.TestCase):
    def test_value(self):
        self.assertEqual(helper(VALUE), 42)
''')
    settings = dict(start='tests', pattern='test*.py', pytest=True, shards={})
    original = collect(project, settings)
    root = project / 'tests' if namespace else project
    source = 'test_preamble.py' if namespace else 'tests/test_preamble.py'
    dest = '_shared.py' if namespace else 'tests/_shared.py'
    prepared_move = subprocess.run([sys.executable, str(SPLIT / 'rope_move.py'), '--project', str(root),
                                   '--source', source, '--dest', dest, '--symbols', 'VALUE,helper', '--apply'],
                                  cwd=root, check=False, capture_output=True, text=True)
    assert prepared_move.returncode == 0, prepared_move.stdout + prepared_move.stderr
    prepared = collect(project, settings)
    compare(original, prepared)
    (project / 'tests-before.json').write_text(json.dumps(prepared))
    commit_all(project)
    before, after, manifest = plan(project, 'tests/test_preamble.py', 'tests/_scenario.py',
                                    'TestPreamble', ['test_value'], shape='mixin', target_class='ScenarioMixin',
                                    test_only=True, test_snapshot='tests-before.json')
    publish(project, before, after, manifest, 'preamble-move.json', True)
    compare(prepared, collect(project, settings))
    subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
                   cwd=project, check=True, capture_output=True, text=True)
    subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests'],
                   cwd=project, check=True, capture_output=True, text=True)


def test_census_ignores_unrelated_patches_and_reflection(tmp_path):
    (tmp_path / 'usage.py').write_text('''from pkg import Engine as E
from unittest.mock import patch
Alias = E
with patch.object(
    Alias,
    "run",
): pass
patch("pkg.Engine.run")
patch("pkg.Unrelated.run")
patch.object(Unrelated, "run")
vars(Unrelated)
Unrelated.__dict__
Alias.run = replacement
Alias.run(instance)
vars(Alias)
Alias.__qualname__
''')
    hits = census(tmp_path, 'Engine')
    assert not any('Unrelated' in h['text'] for h in hits)
    assert any('patch.object(Alias' in h['text'] for h in hits)
    assert any('pkg.Engine.run' in h['text'] for h in hits)
    assert {h['kind'] for h in hits} >= {'patch-or-reflection', 'assignment', 'unbound-call', 'reflection'}


def test_shared_state_hub_does_not_glue_all_methods():
    methods = ''.join(f'    def {seam}_{n}(self): return self.ctx + self.{seam}\n'
                      for seam in ['admission', 'recovery', 'cleanup'] for n in range(4))
    result = inventory('class Engine:\n' + methods, 'Engine')
    assert '@state:ctx' in result['hubs']
    assert max(map(len, result['clusters'])) <= 4
    assert sorted(n for group in result['clusters'] for n in group) == sorted(m['name'] for m in result['methods'])


def test_inventory_exposes_test_preamble_prerequisites():
    source = 'ROOT = 1\ndef helper(): return ROOT\nclass Tests:\n    def test_run(self): return helper() + ROOT\n'
    result = inventory(source, 'Tests')
    assert {p['name'] for p in result['prerequisites']} == {'ROOT', 'helper'}
    assert all(p['users'] == [{'method': 'Tests.test_run', 'line': 4}] for p in result['prerequisites'])


def test_plain_bindings_pruning_and_opt_in_facade_retention(project):
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_recovery.py', 'Engine', ['calculate'])
    assert 'from . import _recovery' in after['sample/engine.py']
    assert 'calculate = _recovery.calculate' in after['sample/engine.py']
    assert 'from math import sqrt' not in after['sample/engine.py']
    assert 'noqa' not in after['sample/engine.py'] and '_decompose_' not in after['sample/engine.py']
    assert 'format_imports' not in manifest['operations'][0]
    verify(before, after, manifest)
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_recovery.py', 'Engine', ['calculate'], retain_module_api=True)
    assert 'from math import sqrt' in after['sample/engine.py'] and 'noqa: F401' in after['sample/engine.py']
    verify(before, after, manifest)


def test_project_declared_plain_decorator_is_wrapped_not_refused(tmp_path):
    import json, sys
    sys.path.insert(0, str(SCRIPTS))
    from class_inventory import inventory
    from quality import config
    source = (
        "import functools\n"
        "def serialize(fn):\n"
        "    @functools.wraps(fn)\n"
        "    def wrapped(self, *a, **k):\n"
        "        return fn(self, *a, **k)\n"
        "    return wrapped\n"
        "class Engine:\n"
        "    @serialize\n"
        "    def run(self):\n"
        "        return 1\n"
    )
    facts = {m['name']: m for m in inventory(source, 'Engine')['methods']}
    assert facts['run']['verdict'] == 'unsupported'
    (tmp_path / '.refactor-quality.json').write_text(json.dumps({'plain_decorators': ['serialize']}))
    declared = config(tmp_path)['plain_decorators']
    facts = {m['name']: m for m in inventory(source, 'Engine', plain_decorators=declared)['methods']}
    assert facts['run']['verdict'] == 'wrap'
    (tmp_path / '.refactor-quality.json').write_text(json.dumps({'plain_decorators': 'serialize'}))
    try:
        config(tmp_path)
    except ValueError as exc:
        assert 'plain_decorators' in str(exc)
    else:
        raise AssertionError('string instead of list accepted')


def _nested_project(tmp_path):
    from conftest import commit_all
    root = tmp_path / 'nested'
    (root / 'pkg').mkdir(parents=True)
    (root / 'pkg' / '__init__.py').write_text('')
    for args in [('init',), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.invalid')]:
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)
    (root / 'pkg' / 'm.py').write_text(
        "def run(items, flag):\n"
        "    total = 0\n"
        "    if flag:\n"
        "        base = len(items)\n"
        "        scaled = base * 2\n"
        "        total = scaled + 1\n"
        "    for item in items:\n"
        "        if item < 0:\n"
        "            break\n"
        "        total += item\n"
        "    return total\n"
    )
    commit_all(root)
    return root


def test_extract_range_inside_nested_block(tmp_path):
    import sys
    sys.path.insert(0, str(SCRIPTS))
    from extract_ranges import plan
    from common import publish
    root = _nested_project(tmp_path)
    before, after, manifest = plan(root, 'pkg/m.py', 'run', '_scale', 4, 5)
    op = manifest['operations'][0]
    assert op['start_line'] == 4 and op['outputs'] == ['scaled']
    assert 'scaled = _scale(' in after['pkg/m.py']
    publish(root, before, after, manifest, '.refactor/nested.json', apply=True)
    ns = {}
    exec((root / 'pkg' / 'm.py').read_text(), ns)
    assert ns['run']([1, 2], True) == 8 and ns['run']([1, -1, 5], False) == 1


def test_extract_refuses_straddling_and_escaping_loop_control(tmp_path):
    import sys
    sys.path.insert(0, str(SCRIPTS))
    from extract_ranges import plan
    from common import Refusal
    root = _nested_project(tmp_path)
    with pytest.raises(Refusal):
        plan(root, 'pkg/m.py', 'run', '_x', 5, 7)      # straddles the if block and the for loop
    with pytest.raises(Refusal):
        plan(root, 'pkg/m.py', 'run', '_x', 8, 9)      # break whose loop is outside the range
    before, after, manifest = plan(root, 'pkg/m.py', 'run', '_loop', 7, 10)  # whole loop, break inside: fine
    assert manifest['operations'][0]['end_line'] == 10


@pytest.mark.parametrize('future', [False, True])
@pytest.mark.parametrize('format_header', [False, True])
def test_annotate_self_descriptors_runtime_and_repeated_destination(project, future, format_header):
    import libcst as cst

    source, dest = 'sample/engine.py', 'sample/_ops.py'
    if future:
        path = project / source
        path.write_text('from __future__ import annotations\n' + path.read_text())
    (project / dest).write_text('"""Operations."""\nfrom typing import Any\n\nsentinel: Any = None\n')
    for index, methods in enumerate([['calculate', 'add', 'build', 'doubled', 'temporary'], ['closure']]):
        commit_all(project)
        plain = plan(project, source, dest, 'Engine', methods, format_header=format_header)[1]
        before, after, manifest = plan(project, source, dest, 'Engine', methods,
                                       annotate_self=True, format_header=format_header)
        assert verify(before, after, manifest)
        op = manifest['operations'][0]
        assert op['annotate_self'] is True
        assert op['type_checking_imports'] == {dest: ['from sample.engine import Engine']}
        assert any('TYPE_CHECKING' in text for text in op['imports'][dest]) == (index == 0)
        assert after[source] == plain[source]  # The bindings and decorators are unchanged.
        tree = ast.parse(after[dest])
        blocks = [n for n in tree.body if isinstance(n, ast.If)]
        assert len(blocks) == 1 and tree.body.index(blocks[0]) == header_range(tree)[1]
        assert ast.unparse(blocks[0]) == 'if TYPE_CHECKING:\n    from sample.engine import Engine'
        original = next(n for n in cst.parse_module(before[source]).body if isinstance(n, cst.ClassDef))
        for method in methods:
            fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == method)
            annotation = fn.args.args[0].annotation
            if method == 'add':
                assert annotation is None
            else:
                assert isinstance(annotation, ast.Constant)
                assert annotation.value == ('type[Engine]' if method == 'build' else 'Engine')
            old = next(n for n in original.body.body if isinstance(n, cst.FunctionDef) and n.name.value == method)
            new = next(n for n in cst.parse_module(after[dest]).body if isinstance(n, cst.FunctionDef) and n.name.value == method)
            assert old.body.deep_equals(new.body)
        publish(project, before, after, manifest, f'.refactor/annotated-{index}.json', True)
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
                       cwd=project, check=True, capture_output=True, text=True)
        if format_header:
            assert 'from typing import Any, TYPE_CHECKING' in after[dest] or 'from typing import TYPE_CHECKING, Any' in after[dest]
            subprocess.run([sys.executable, '-m', 'ruff', 'check', '--select', 'I', source, dest],
                           cwd=project, check=True, capture_output=True, text=True)


def test_annotate_self_plain_decorator_and_parameter_rules(project):
    (project / '.refactor-quality.json').write_text(json.dumps({'plain_decorators': ['serialize']}))
    source = '''def serialize(fn):
    return fn

class Engine:
    @serialize
    def decorated(receiver, /, value):
        return value

    def annotated(receiver: object):
        return receiver

    def no_positional(*, value):
        return value

    @staticmethod
    def static(self):
        return self

    @classmethod
    def build(receiver, /):
        return receiver()

    async def async_method(receiver):
        return receiver
'''
    (project / 'sample/custom.py').write_text(source)
    methods = ['decorated', 'annotated', 'no_positional', 'static', 'build', 'async_method']
    before, after, manifest = plan(project, 'sample/custom.py', 'sample/_custom.py', 'Engine', methods,
                                   annotate_self=True)
    assert verify(before, after, manifest)
    functions = {n.name: n for n in ast.parse(after['sample/_custom.py']).body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert functions['decorated'].args.posonlyargs[0].annotation.value == 'Engine'
    assert ast.unparse(functions['annotated'].args.args[0].annotation) == 'object'
    assert functions['no_positional'].args.kwonlyargs[0].annotation is None
    assert functions['static'].args.args[0].annotation is None
    assert functions['build'].args.posonlyargs[0].annotation.value == 'type[Engine]'
    assert functions['async_method'].args.args[0].annotation.value == 'Engine'
    assert 'decorated = serialize(_custom.decorated)' in after['sample/custom.py']
    commit_all(project)
    publish(project, before, after, manifest, '.refactor/custom.json', True)
    subprocess.run([sys.executable, '-c', 'from sample.custom import Engine; '
                    'e = Engine(); assert e.decorated(3) == 3 and e.annotated() is e; '
                    'assert e.static(4) == 4 and isinstance(e.build(), Engine)'],
                   cwd=project, check=True, capture_output=True, text=True)


@pytest.mark.parametrize('annotate_self', [False, True])
def test_annotate_self_preserves_mypy_checks_across_two_destinations(project, annotate_self):
    source = '''class Engine:
    ctx: int = 1

    def first(self, depth: int) -> int:
        if depth:
            return self.second(depth - 1)
        return self.ctxx

    def second(self, depth: int) -> int:
        if depth:
            return self.first(depth - 1)
        return self.ctx
'''
    (project / 'sample/typed.py').write_text(source)

    def errors():
        result = subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', '--show-error-codes', 'sample'],
                                cwd=project, capture_output=True, text=True)
        assert result.returncode in (0, 1), result.stdout + result.stderr
        return [line for line in result.stdout.splitlines() if ': error:' in line]

    baseline = errors()
    assert len(baseline) == 1 and '"ctxx"' in baseline[0] and '[attr-defined]' in baseline[0]
    for method in ['first', 'second']:
        commit_all(project)
        before, after, manifest = plan(project, 'sample/typed.py', f'sample/_{method}.py', 'Engine', [method],
                                       annotate_self=annotate_self, format_header=True)
        publish(project, before, after, manifest, f'.refactor/{method}.json', True)
    final = errors()
    if annotate_self:
        assert len(final) == 1 and '"ctxx"' in final[0] and '[attr-defined]' in final[0]
        assert final[0].startswith('sample/_first.py:')
    else:
        assert final == []


@pytest.mark.parametrize('shape', ['mixin', 'class'])
def test_annotate_self_refuses_other_shapes(project, shape):
    settings = dict(start='tests', top='.', pattern='test*.py', pytest=True, shards={})
    (project / 'ids.json').write_text(json.dumps(collect(project, settings)))
    with pytest.raises(Refusal, match='annotate.self.*function'):
        plan(project, 'tests/test_engine.py', 'tests/_support.py', 'TestEngine', ['test_summary'],
             shape=shape, target_class='SummaryMixin', test_only=True, test_snapshot='ids.json', annotate_self=True)
    result = subprocess.run([sys.executable, str(SCRIPTS / 'move_methods.py'), '--source', 'sample/engine.py',
                             '--dest', 'sample/_ops.py', '--class', 'Engine', '--methods', 'calculate',
                             '--shape', shape, '--annotate-self', '--manifest', '.refactor/refused.json'],
                            cwd=project, capture_output=True, text=True)
    assert result.returncode == 1 and '--annotate-self requires --shape function' in result.stderr


def test_annotate_self_cli_and_foreign_block_refusal(project):
    dest = project / 'sample/_ops.py'
    command = [sys.executable, str(SCRIPTS / 'move_methods.py'), '--source', 'sample/engine.py',
               '--dest', 'sample/_ops.py', '--class', 'Engine', '--methods', 'calculate',
               '--annotate-self', '--format-imports', '--manifest', '.refactor/annotated.json', '--apply']
    dest.write_text('from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from sample.engine import Other\n')
    commit_all(project)
    result = subprocess.run(command, cwd=project, capture_output=True, text=True)
    assert result.returncode == 1 and 'merge by hand first' in result.stderr
    dest.write_text('')
    commit_all(project)
    result = subprocess.run(command, cwd=project, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'def calculate(self: "Engine"):' in dest.read_text()
    assert json.loads((project / '.refactor/annotated.json').read_text())['operations'][0]['annotate_self'] is True


def test_annotate_self_oracle_rejects_wrong_manifest_and_header(project):
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_ops.py', 'Engine', ['calculate'],
                                   annotate_self=True)
    wrong = copy.deepcopy(manifest)
    wrong['operations'][0]['type_checking_imports']['sample/_ops.py'] = ['from sample.engine import Other']
    with pytest.raises(Refusal, match='undeclared structural change'):
        verify(before, after, wrong)
    bad_header = {**after, 'sample/_ops.py': after['sample/_ops.py'].replace('import Engine', 'import Other')}
    with pytest.raises(Refusal, match='undeclared structural change'):
        verify(before, bad_header, manifest)


@pytest.mark.parametrize('shape', ['mixin', 'class'])
def test_annotate_self_oracle_itself_refuses_other_shapes(project, shape):
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_ops.py', 'Engine', ['calculate'])
    manifest['operations'][0].update(annotate_self=True, shape=shape, test_only=True, target_class='Helper')
    with pytest.raises(Refusal, match='annotate.self.*function'):
        expected(before, manifest)


@pytest.mark.parametrize('statements', [
    [], ['import sample.engine'], ['from sample.engine import Engine; import os'],
    ['from sample.engine import Engine', 'from sample.engine import Other'],
    ['not python'], 'from sample.engine import Engine', [42],
])
def test_annotate_self_oracle_itself_validates_type_import(project, statements):
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_ops.py', 'Engine', ['calculate'])
    manifest['operations'][0]['type_checking_imports'] = {'sample/_ops.py': statements}
    with pytest.raises(Refusal, match='single.*from.*import'):
        expected(before, manifest)


@pytest.mark.parametrize('imported', ['Engine', 'Other'])
def test_annotate_self_oracle_itself_checks_existing_block(project, imported):
    # Build a plain manifest first: the mover's foreign-block guard must not mask this check.
    (project / 'sample/_ops.py').write_text(f'from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from sample.engine import {imported}\n')
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/_ops.py', 'Engine', ['calculate'])
    manifest['operations'][0].update(annotate_self=True,
                                     type_checking_imports={'sample/_ops.py': ['from sample.engine import Engine']})
    if imported == 'Other':
        with pytest.raises(Refusal, match='merge by hand first'):
            expected(before, manifest)
    else:
        tree = expected(before, manifest)['sample/_ops.py']
        assert len([n for n in tree.body if isinstance(n, ast.If)]) == 1
