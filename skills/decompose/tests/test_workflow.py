"""Run the workflow in a stub host to exercise stop/completion behavior."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[3] / 'workflows/decompose.js'


@pytest.mark.skipif(not shutil.which('node'), reason='workflow host syntax check requires Node')
@pytest.mark.parametrize('failure,expected', [
    ('none', 'done'), ('review', 'review-required'), ('codex', 'review-required'),
    ('preflight', 'error'), ('critique', 'error'), ('extract', 'error'),
    ('merge', 'error'), ('finalize', 'error'),
])
def test_workflow_requires_gates_and_reviews(failure, expected):
    code = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8').replace('export const meta', 'const meta');
const failure = process.argv[2];
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const workflow = new AsyncFunction('args','phase','agent',source);
const seen = [];
const agent = async (prompt, opts) => {
  seen.push(opts.label);
  if (opts.label === 'plan' || opts.label === 'replan') return {path:'plan.md',clusters:[
    {id:'a',tier:1,tool:'move_methods.py',arguments:{},testMode:'none'}]};
  if (opts.label.startsWith('critique')) return failure === 'critique' ? null : {verdict:'APPROVE',blocking:[]};
  if (['review','codex'].includes(opts.label)) return {verdict:failure === opts.label ? 'REJECT' : 'APPROVE',blocking:[]};
  if (opts.label.split(':')[0] === failure) return {gate:'FAIL',cause:'fixture failure'};
  return {gate:'PASS',commit:'0123456789',snapshot:'before.json',manifest:'move.json'};
};
workflow({targets:[{source:'engine.py',className:'Engine'}],pkgDir:'.'},()=>{},agent)
 .then(result=>console.log(JSON.stringify({status:result.status,seen})))
 .catch(error=>console.log(JSON.stringify({status:'error',message:error.message,seen})));
"""
    result = subprocess.run(['node', '-e', code, str(WORKFLOW), failure], capture_output=True,
                            text=True, check=True)
    data = json.loads(result.stdout)
    assert data['status'] == expected
    if failure == 'merge':
        assert 'finalize' not in data['seen']
