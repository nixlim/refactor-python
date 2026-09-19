"""Snapshot test IDs and explicit shard memberships in isolated collector processes."""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path

from common import Refusal


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item.id()


def worker(settings, framework):
    sys.path.insert(0, str(Path.cwd()))
    sys.path.insert(0, str(Path(settings['top']).resolve()))
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        if framework == 'unittest':
            loader = unittest.TestLoader()
            if settings.get('modules'):
                suite = loader.loadTestsFromNames(settings['modules'])
            else:
                suite = loader.discover(settings['start'], pattern=settings['pattern'], top_level_dir=settings['top'])
            if loader.errors:
                raise Refusal('\n'.join(loader.errors))
            ids = list(flatten(suite))
        else:
            import pytest
            class Collector:
                def __init__(self):
                    self.ids = []
                    self.patterns = {}

                def pytest_collection_finish(self, session):
                    self.ids = [item.nodeid for item in session.items]
                    self.patterns = {'files': session.config.getini('python_files'),
                                     'classes': session.config.getini('python_classes')}
            collector = Collector()
            code = pytest.main([*settings.get('pytest_args', [settings['start']]), '--collect-only', '-q',
                                '-p', 'no:cacheprovider'], plugins=[collector])
            if code not in (0, 5):
                raise Refusal(f'pytest collection failed ({code}): {output.getvalue()}')
            ids = collector.ids
    if len(ids) != len(set(ids)):
        raise Refusal(f'duplicate {framework} test IDs')
    return {'ids': sorted(ids), 'patterns': collector.patterns if framework == 'pytest' else {}}


def collect(root, settings):
    settings = dict(settings)
    if not settings.get('top'):
        preset = settings.get('preset', 'auto')
        namespace = preset == 'namespace' or (preset == 'auto' and not (Path(root) / settings['start'] / '__init__.py').exists())
        settings['top'] = settings['start'] if namespace else '.'
    def run(framework, overrides=None):
        current = {**settings, **(overrides or {})}
        env = {**os.environ, **current.get('env', {}), 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--worker', framework,
                                 '--settings', json.dumps(current)], cwd=root, env=env,
                                text=True, capture_output=True)
        if result.returncode:
            raise Refusal(result.stderr or result.stdout)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise Refusal(f'collector did not return JSON: {result.stdout}') from exc
    frameworks = ['unittest']
    if settings.get('pytest', True) and importlib.util.find_spec('pytest'):
        frameworks.append('pytest')
    collected = {f: run(f) for f in frameworks}
    ids = {f: result['ids'] for f, result in collected.items()}
    for framework, pinned in settings.get('pinned', {}).items():
        if framework not in ids or not set(pinned) <= set(ids[framework]):
            raise Refusal('pinned test IDs were not collected')
    shards = {name: run('unittest', overrides)['ids'] for name, overrides in settings.get('shards', {}).items()}
    return dict(version=1, settings=settings, ids=ids, shards=shards,
                patterns=collected.get('pytest', {}).get('patterns', {}))


def compare(before, after, mode='identity', mapping=None):
    mapping = mapping or {}
    if set(mapping) - set(before['ids']):
        raise Refusal('mapping names an unknown collector')
    if before.get('patterns') != after.get('patterns'):
        raise Refusal('test discovery patterns changed')
    if set(before['ids']) != set(after['ids']) or set(before['shards']) != set(after['shards']):
        raise Refusal('collector/shard set changed')
    if mode == 'identity' and any(mapping.values()):
        raise Refusal('identity mode does not accept ID remapping')
    for framework, ids in before['ids'].items():
        remap = mapping.get(framework, {}) if mode == 'mapping' else {}
        if set(remap) - set(ids) or len(set(remap.values())) != len(remap):
            raise Refusal(f'invalid/non-injective {framework} test mapping')
        if set(remap) & set(before['settings'].get('pinned', {}).get(framework, [])):
            raise Refusal('mapping changes a pinned test ID')
        expected = [remap.get(i, i) for i in ids]
        if len(set(expected)) != len(expected) or Counter(expected) != Counter(after['ids'][framework]):
            raise Refusal(f'{framework} test IDs/counts changed outside mapping')
    for name, ids in before['shards'].items():
        remap = mapping.get('unittest', {}) if mode == 'mapping' else {}
        if Counter(remap.get(i, i) for i in ids) != Counter(after['shards'][name]):
            raise Refusal(f'shard membership changed: {name}')
    return True


def validate_support(path, class_name, shape, patterns=('test*.py', '*_test.py'), class_patterns=('Test*',)):
    if not class_name or not class_name.isidentifier():
        raise Refusal('test destination needs a valid class name')
    if shape == 'mixin':
        if any(fnmatch.fnmatch(Path(path).name, pattern) for pattern in patterns):
            raise Refusal('support module matches a test discovery pattern')
        if any(fnmatch.fnmatch(class_name, pattern) or ('*' not in pattern and class_name.startswith(pattern))
               for pattern in class_patterns):
            raise Refusal('mixin class matches collector class pattern')


def validate_test_scope(root, source, class_name, snapshot):
    if not snapshot:
        raise Refusal('test shapes require --test-snapshot evidence')
    before = json.loads((Path(root) / snapshot).read_text())
    source_path = (Path(root) / source).resolve()
    top = (Path(root) / before['settings'].get('top', '.')).resolve()
    stem = str(source_path.relative_to(top).with_suffix('')).replace(os.sep, '.')
    unit_prefix = f'{stem}.{class_name}.'
    pytest_prefix = f'{Path(source).as_posix()}::{class_name}::'
    if not any(i.startswith(unit_prefix) for i in before['ids'].get('unittest', [])) and not any(
            i.startswith(pytest_prefix) for i in before['ids'].get('pytest', [])):
        raise Refusal('selected class has no collected tests in the snapshot')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['snapshot', 'compare'])
    parser.add_argument('snapshot', nargs='?')
    parser.add_argument('--project', default='.')
    parser.add_argument('--start', default='tests')
    parser.add_argument('--top', help='explicit unittest top level; default inferred from tests/__init__.py')
    parser.add_argument('--preset', choices=['auto', 'package', 'namespace'], default='auto')
    parser.add_argument('--pattern', default='test*.py')
    parser.add_argument('--no-pytest', action='store_true')
    parser.add_argument('--shards', help='JSON {name: {modules: [...], env: {...}}}')
    parser.add_argument('--pinned', help='JSON {unittest: [...], pytest: [...]} of IDs that must never change')
    parser.add_argument('--out')
    parser.add_argument('--mode', choices=['identity', 'mapping'], default='identity')
    parser.add_argument('--manifest')
    parser.add_argument('--worker', choices=['unittest', 'pytest'])
    parser.add_argument('--settings')
    args = parser.parse_args()
    try:
        if args.worker:
            print(json.dumps(worker(json.loads(args.settings), args.worker)))
        elif args.command == 'snapshot':
            if not args.out:
                raise Refusal('snapshot requires --out')
            settings = dict(start=args.start, top=args.top, preset=args.preset, pattern=args.pattern, pytest=not args.no_pytest,
                            shards=json.loads(Path(args.shards).read_text()) if args.shards else {},
                            pinned=json.loads(Path(args.pinned).read_text()) if args.pinned else {})
            result = collect(args.project, settings)
            Path(args.out).write_text(json.dumps(result, indent=2) + '\n')
            print('test snapshot:', {k: len(v) for k, v in result['ids'].items()})
        elif args.command == 'compare':
            before = json.loads(Path(args.snapshot).read_text())
            manifest = json.loads(Path(args.manifest).read_text()) if args.manifest else {}
            compare(before, collect(args.project, before['settings']), args.mode, manifest.get('test_id_map'))
            print('test IDs and shard memberships: PASS')
        else:
            parser.error('specify snapshot or compare')
    except (Refusal, ValueError, OSError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
