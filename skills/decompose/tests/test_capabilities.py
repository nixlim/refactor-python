"""End-to-end checks of structural contracts, runtime behavior, and refusals."""
import ast
import copy
import difflib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import commit_all

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from class_inventory import inventory
from collect_tests import collect, compare, validate_support
from common import Refusal, publish
from extract_ranges import plan as extract_plan
from function_inventory import inventory as function_inventory
from manifest_oracle import verify
from move_methods import plan
from quality import config, report


def run_tests(project):
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
                            cwd=project, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def snapshot(project):
    settings = dict(start='tests', top='.', pattern='test*.py', pytest=True,
                    shards={str(i): dict(env={'EXAMPLE_SHARD': str(i)}) for i in range(2)})
    result = collect(project, settings)
    (project / 'ids.json').write_text(json.dumps(result))
    return result


def test_inventory_and_census(project):
    source = (project / 'sample/engine.py').read_text()
    result = inventory(source, 'Engine', project)
    methods = {m['name']: m for m in result['methods']}
    assert methods['calculate']['reads'] == ['factor', 'value']
    assert methods['calculate']['globals'] == ['sqrt']
    assert methods['doubled']['verdict'] == 'wrap'
    assert methods['private_reader']['verdict'] == 'unsupported'
    assert methods['nonlocal_closure']['nonlocal_names'] == ['value']
    assert any('patch.object' in h['text'] for h in result['census'])
    assert any(h['kind'] == 'unbound-call' for h in result['census'])
    blocks = function_inventory(source, 'summarize')['blocks']
    assert any('total' in b['output_candidates'] for b in blocks)
    assert any(b['control'] for b in blocks)


def test_function_move_descriptors_and_oracle(project, capsys):
    methods = ['calculate', 'add', 'build', 'doubled', 'temporary', 'closure', 'nonlocal_closure']
    before, after, manifest = plan(project, 'sample/engine.py', 'sample/operations.py', 'Engine', methods)
    assert verify(before, after, manifest)
    publish(project, before, after, manifest, 'move.json')
    assert not (project / 'move.json').exists()
    assert not (project / 'sample/operations.py').exists()
    publish(project, before, after, manifest, 'move.json', True)
    assert '# Keep this comment' in (project / 'sample/operations.py').read_text()
    run_tests(project)
    for path, old, new in [('sample/operations.py', 'return left + right', 'return left - right'),
                           ('sample/engine.py', 'factor = 2', 'factor = 3')]:
        broken = {**after, path: after[path].replace(old, new)}
        with pytest.raises(Refusal, match='undeclared'):
            verify(before, broken, manifest)
    with pytest.raises(Refusal, match='baseline'):
        verify({**before, 'sample/engine.py': before['sample/engine.py'] + '\n'}, after, manifest)


@pytest.mark.parametrize('method', ['private_reader', '__hidden'])
def test_mangling_refused(project, method):
    with pytest.raises(Refusal, match='mangling'):
        plan(project, 'sample/engine.py', 'sample/helpers.py', 'Engine', [method])


@pytest.mark.parametrize('source,reason', [
    ('class A:\n def run(self): return super().run()\n', 'super'),
    ('class A:\n @unknown\n def run(self): return 1\n', 'decorator'),
    ('class A:\n x = 1\n def run(self, value=x): return value\n', 'class-dependent'),
    ('class A:\n __slots__ = ()\n def run(self): return 1\n', 'slots'),
    ('class A(metaclass=Meta):\n def run(self): return 1\n', 'metaclass'),
    ('class A:\n def run(self): return globals()\n', 'dynamic'),
    ('VALUE = 1\nclass A:\n def run(self): return VALUE\n', 'independent owner'),
])
def test_hazards(project, source, reason):
    (project / 'hazard.py').write_text(source)
    with pytest.raises(Refusal, match=reason):
        plan(project, 'hazard.py', 'helpers.py', 'A', ['run'])


def test_destination_clash_and_production_mixin(project):
    (project / 'sample/helpers.py').write_text('def calculate(): pass\n')
    with pytest.raises(Refusal, match='clash'):
        plan(project, 'sample/engine.py', 'sample/helpers.py', 'Engine', ['calculate'])
    with pytest.raises(Refusal, match='test classes'):
        plan(project, 'sample/engine.py', 'sample/mixin.py', 'Engine', ['calculate'], 'mixin', 'CalculationMixin')


def test_mixin_preserves_ids_and_shards(project):
    ids = snapshot(project)
    before, after, manifest = plan(project, 'tests/test_engine.py', 'tests/_support.py', 'TestEngine',
                                  ['test_calculate', 'test_patch'], 'mixin', 'CalculationMixin', True,
                                  test_snapshot='ids.json')
    publish(project, before, after, manifest, 'mixin.json', True)
    compare(ids, collect(project, ids['settings']))
    run_tests(project)
    bad = copy.deepcopy(ids)
    bad['shards']['0'] = []
    with pytest.raises(Refusal, match='shard'):
        compare(ids, bad)


def test_class_move_id_mapping(project):
    ids = snapshot(project)
    before, after, manifest = plan(project, 'tests/test_engine.py', 'tests/test_summaries.py', 'TestEngine',
                                  ['test_summary'], 'class', 'TestSummaries', True, test_snapshot='ids.json')
    publish(project, before, after, manifest, 'class.json', True)
    new = collect(project, {**ids['settings'], 'shards': {}})
    old = {**ids, 'shards': {}}
    mapping = {'unittest': {'tests.test_engine.TestEngine.test_summary': 'tests.test_summaries.TestSummaries.test_summary'},
               'pytest': {'tests/test_engine.py::TestEngine::test_summary': 'tests/test_summaries.py::TestSummaries::test_summary'}}
    compare(old, new, 'mapping', mapping)
    with pytest.raises(Refusal, match='IDs'):
        compare(old, new)
    run_tests(project)


@pytest.mark.parametrize('path,name', [('tests/test_support.py', 'HelperMixin'), ('tests/_support.py', 'TestMixin')])
def test_discovery_refusals(path, name):
    with pytest.raises(Refusal, match='pattern'):
        validate_support(path, name, 'mixin')


def test_extract_and_hoist(project):
    source = (project / 'sample/engine.py').read_text()
    fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'summarize')
    before, after, manifest = extract_plan(project, 'sample/engine.py', 'summarize', 'weighted_total',
                                           fn.body[0].lineno, fn.body[1].end_lineno)
    publish(project, before, after, manifest, 'extract.json', True)
    run_tests(project)
    commit_all(project)
    before, after, manifest = extract_plan(project, 'sample/engine.py', 'Engine.closure', 'add_closure_value', nested='add_value')
    publish(project, before, after, manifest, 'hoist.json', True)
    run_tests(project)


def test_extract_refusals(project):
    source = (project / 'sample/engine.py').read_text()
    fn = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'summarize')
    with pytest.raises(Refusal, match='return'):
        extract_plan(project, 'sample/engine.py', 'summarize', 'bad_return', fn.body[-2].lineno, fn.body[-1].end_lineno)
    with pytest.raises(Refusal, match='complete consecutive'):  # straddles the loop body and the statement after it
        extract_plan(project, 'sample/engine.py', 'summarize', 'partial', fn.body[1].lineno + 1, fn.body[2].end_lineno)
    with pytest.raises(Refusal, match='parameters'):
        extract_plan(project, 'sample/engine.py', 'summarize', 'too_many', fn.body[0].lineno, fn.body[1].end_lineno, max_parameters=1)
    with pytest.raises(Refusal, match='nonlocal'):
        extract_plan(project, 'sample/engine.py', 'Engine.nonlocal_closure', 'increment_value', nested='increment')


def test_quality(project):
    (project / '.refactor-quality.json').write_text(json.dumps(dict(module_target=10, module_ceiling=100)))
    limits = config(project)
    result = report((project / 'sample/engine.py').read_text(), limits)
    assert result['debt'] and not result['over_ceiling']
    (project / '.refactor-quality.json').write_text('{"module_target": 1000}')
    with pytest.raises(ValueError, match='ceiling'):
        config(project)


@pytest.mark.parametrize('operation', ['move', 'extract'])
@pytest.mark.parametrize('format_header', [False, True])
def test_terminal_newline_and_dry_run_match_apply(project, capsys, operation, format_header):
    source, dest = 'sample/engine.py', 'sample/_ops.py'
    original = (project / source).read_bytes()
    if operation == 'move':
        before, after, manifest = plan(project, source, dest, 'Engine', ['calculate'],
                                       format_header=format_header)
    else:
        fn = next(n for n in ast.parse(original).body if isinstance(n, ast.FunctionDef) and n.name == 'summarize')
        before, after, manifest = extract_plan(project, source, 'summarize', 'weighted_total',
                                               fn.body[0].lineno, fn.body[1].end_lineno,
                                               dest=dest, format_header=format_header)
    for text in after.values():
        assert text.endswith('\n') and not text.endswith('\n\n')
    assert after[source].encode().endswith(original.splitlines(keepends=True)[-1])
    publish(project, before, after, manifest, '.refactor/newline.json')
    diff = ''.join(''.join(difflib.unified_diff(before[p].splitlines(True), after[p].splitlines(True),
                                              fromfile=p, tofile=p)) for p in after)
    assert capsys.readouterr().out.startswith(diff + '{\n')
    assert not (project / dest).exists()
    publish(project, before, after, manifest, '.refactor/newline.json', True)
    assert all((project / p).read_bytes() == text.encode() for p, text in after.items())


def test_second_move_keeps_one_terminal_newline(project):
    for method in ['calculate', 'closure']:
        before, after, manifest = plan(project, 'sample/engine.py', 'sample/_ops.py', 'Engine', [method])
        publish(project, before, after, manifest, f'.refactor/{method}.json', True)
        assert (project / 'sample/_ops.py').read_bytes().endswith(b'\n')
        assert not (project / 'sample/_ops.py').read_bytes().endswith(b'\n\n')
        commit_all(project)


@pytest.mark.parametrize('future', [False, True])
@pytest.mark.parametrize('body,needed', [
    ('        data: dict[str, Any] = {}\n        print(data)\n', {'Any'}),
    ('        data: "dict[str, Any]" = {}\n        print(data)\n', {'Any'}),
    ('        def nested(value: "Any") -> "Any":\n'
     '            result: "Any" = value\n            return result\n'
     '        print(nested(1))\n', {'Any'}),
    ('        def nested(value: "Any") -> "Callable[[], Any]":\n'
     '            return lambda: value\n        print(nested(1)())\n', {'Any', 'Callable'}),
])
def test_move_copies_annotation_dependencies(project, future, body, needed):
    source, dest = 'sample/annotations.py', 'sample/_annotations.py'
    (project / source).write_text(('from __future__ import annotations\n' if future else '') +
                                 'from typing import Any, Callable\n\nclass E:\n'
                                 '    def run(self) -> None:\n' + body +
                                 '    def stay(self) -> None:\n'
                                 '        data: "Any" = 1\n        print(data)\n')
    commit_all(project)
    before, after, manifest = plan(project, source, dest, 'E', ['run'], format_header=True)
    assert verify(before, after, manifest)
    imports = {a.name for n in ast.parse(after[dest]).body if isinstance(n, ast.ImportFrom)
               and n.module == 'typing' for a in n.names}
    assert imports == needed
    assert 'from typing import Any' in after[source]  # The remaining string annotation still needs it.
    publish(project, before, after, manifest, '.refactor/annotations.json', True)
    for tool_args in [('mypy', '--no-incremental', '--check-untyped-defs'), ('ruff', 'check', '--select', 'F821')]:
        result = subprocess.run([sys.executable, '-m', *tool_args, source, dest],
                                cwd=project, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run([sys.executable, '-c', 'from sample.annotations import E; E().run(); E().stay()'],
                   cwd=project, check=True, capture_output=True)


@pytest.mark.parametrize('future', [False, True])
@pytest.mark.parametrize('quoted', [False, True])
def test_extract_copies_annotation_dependencies(project, future, quoted):
    source, dest = 'sample/annotations.py', 'sample/_annotations.py'
    annotation = '"dict[str, Any]"' if quoted else 'dict[str, Any]'
    (project / source).write_text(('from __future__ import annotations\n' if future else '') +
                                 'from typing import Any\n\ndef run() -> None:\n'
                                 f'    data: {annotation} = {{}}\n    print(data)\n')
    commit_all(project)
    fn = next(n for n in ast.parse((project / source).read_text()).body if isinstance(n, ast.FunctionDef))
    before, after, manifest = extract_plan(project, source, 'run', 'show_data',
                                           fn.body[0].lineno, fn.body[1].end_lineno, dest=dest)
    assert verify(before, after, manifest)
    assert 'from typing import Any' in after[dest]
    publish(project, before, after, manifest, '.refactor/annotations.json', True)
    for tool_args in [('mypy', '--no-incremental', '--check-untyped-defs'), ('ruff', 'check', '--select', 'F821')]:
        result = subprocess.run([sys.executable, '-m', *tool_args, source, dest],
                                cwd=project, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


def test_annotation_dependencies_respect_scope_and_literal_values(project):
    source, dest = 'sample/annotations.py', 'sample/_annotations.py'
    (project / source).write_text('''from __future__ import annotations
from typing import Annotated as A, Literal as L

class E:
    def run(self) -> None:
        from typing import Literal as LocalLiteral
        class Local:
            pass
        def nested(value: "Local") -> "Local":
            data: "Local" = value
            return data
        values: "list[Local]" = [nested(Local())]
        builtin: "dict[str, int]" = {}
        state: "L['ready']" = 'ready'
        marked: "A[int, 'metadata']" = 1
        local_state: "LocalLiteral['ready']" = 'ready'
        print(values, builtin, state, marked, local_state)
''')
    commit_all(project)
    before, after, manifest = plan(project, source, dest, 'E', ['run'])
    assert verify(before, after, manifest)
    assert manifest['operations'][0]['imports'][dest] == [
        'from __future__ import annotations', 'from typing import Annotated as A, Literal as L']
    publish(project, before, after, manifest, '.refactor/annotations.json', True)
    for tool_args in [('mypy', '--no-incremental', '--check-untyped-defs'), ('ruff', 'check', '--select', 'F821')]:
        result = subprocess.run([sys.executable, '-m', *tool_args, source, dest],
                                cwd=project, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr


def test_annotation_parameters_and_shadowed_locals_are_not_imports():
    from move_methods import dependency_imports

    source = '''from __future__ import annotations
from external import Parameter, Local
def run(Parameter):
    class Local: pass
    parameter: "Parameter"
    local: "Local"
    builtin: "list[int]"
'''
    assert dependency_imports(source, ['run'], 'sample.annotations') == ['from __future__ import annotations']


def test_annotation_import_shadowing_builtin_is_copied():
    from move_methods import dependency_imports

    source = ('from __future__ import annotations\nfrom decimal import Decimal as int\n'
              'def run():\n    value: "int"\n')
    assert dependency_imports(source, ['run'], 'sample.annotations') == [
        'from __future__ import annotations', 'from decimal import Decimal as int']


@pytest.mark.parametrize('owner', ['Payload', 'int'])
@pytest.mark.parametrize('quoted', [False, True])
def test_annotation_dependency_still_needs_independent_owner(project, owner, quoted):
    annotation = f'"{owner}"' if quoted else owner
    (project / 'sample/annotations.py').write_text('from __future__ import annotations\n'
                                                 f'class {owner}: pass\nclass E:\n'
                                                 f'    def run(self):\n        data: {annotation}\n')
    with pytest.raises(Refusal, match=rf'independent owner.*{owner}: E.run'):
        plan(project, 'sample/annotations.py', 'sample/_annotations.py', 'E', ['run'])
