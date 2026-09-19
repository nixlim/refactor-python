"""Regression and direct oracle tests; no mover precondition masks these guards."""
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
from manifest_oracle import verify, verify_allowed_methods, verify_scope
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
