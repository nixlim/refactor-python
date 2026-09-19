"""Real git baselines for the worktree-based decomposition tools."""
import shutil
import subprocess
from pathlib import Path

import pytest


def commit_all(project):
    subprocess.run(['git', 'add', '.'], cwd=project, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '--allow-empty', '-m', 'fixture baseline'], cwd=project,
                   check=True, capture_output=True)


@pytest.fixture
def project(tmp_path):
    shutil.copytree(Path(__file__).parent / 'fixture', tmp_path, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    for args in [('init',), ('config', 'user.name', 'Fixture'), ('config', 'user.email', 'fixture@example.invalid')]:
        subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True)
    commit_all(tmp_path)
    return tmp_path
