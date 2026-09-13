#!/usr/bin/env bash
# Preflight for refactor-python. Verifies the toolchain and repo state.
#   preflight.sh            -> report only (exit 1 if a REQUIRED tool is missing)
#   preflight.sh --install  -> try to install missing Python tools into the active env
set -u
INSTALL=0
[ "${1:-}" = "--install" ] && INSTALL=1

ok=(); missing=(); optional_missing=()
PY=${PYTHON:-python3}

have_cmd() { command -v "$1" >/dev/null 2>&1; }
have_mod() { "$PY" -c "import $1" >/dev/null 2>&1; }

pip_install() {
  if have_cmd uv; then uv pip install "$@" 2>/dev/null || "$PY" -m pip install -q "$@"; else "$PY" -m pip install -q "$@"; fi
}

check() { # name kind(cmd|mod) required(1|0) pipname
  local name=$1 kind=$2 req=$3 pipname=${4:-$1}
  local present=0
  if [ "$kind" = cmd ]; then have_cmd "$name" && present=1; else have_mod "$name" && present=1; fi
  if [ $present -eq 0 ] && [ $INSTALL -eq 1 ] && [ -n "$pipname" ]; then
    echo "installing $pipname ..." >&2; pip_install "$pipname"
    if [ "$kind" = cmd ]; then have_cmd "$name" && present=1; else have_mod "$name" && present=1; fi
  fi
  if [ $present -eq 1 ]; then ok+=("$name"); elif [ "$req" = 1 ]; then missing+=("$name"); else optional_missing+=("$name"); fi
}

echo "== toolchain =="
check git cmd 1 ""
check "$PY" cmd 1 ""
check rope mod 1 rope                 # deterministic move engine
check ruff cmd 1 ruff                 # lint / import fix / complexity rules
check pytest cmd 1 pytest             # test gate
# one type checker is enough: prefer whichever is already present
if have_cmd mypy || have_cmd pyright; then ok+=("typechecker"); else
  if [ $INSTALL -eq 1 ]; then pip_install mypy; fi
  have_cmd mypy && ok+=("typechecker") || missing+=("typechecker(mypy|pyright)")
fi
check lint-imports cmd 0 import-linter # module boundary / cycle contracts
check grimp mod 0 grimp               # import graph queries
check radon cmd 0 radon               # complexity ranking
check vulture cmd 0 vulture           # dead code
check jq cmd 0 ""                     # hooks parse JSON faster with jq, optional

echo "present : ${ok[*]:-none}"
echo "missing : ${missing[*]:-none}"
echo "optional: ${optional_missing[*]:-none}"

echo "== codex (second-opinion review) =="
CODEX=$(command -v codex 2>/dev/null); [ -z "$CODEX" ] && CODEX=$(find ~/.cursor/extensions ~/.vscode/extensions ~/.vscode-server/extensions -maxdepth 4 -name codex -type f 2>/dev/null | head -1)
if [ -n "$CODEX" ]; then echo "codex=$CODEX ($("$CODEX" --version 2>/dev/null | head -1))"; else echo "WARN: codex not found; Phase 6a (GPT-5.6 Sol review) will be skipped. Install Codex CLI or the IDE extension, and the codex-orchestrator plugin."; fi
echo "== repo state =="
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  branch=$(git rev-parse --abbrev-ref HEAD)
  dirty=$(git status --porcelain | wc -l | tr -d ' ')
  echo "branch=$branch dirty_files=$dirty"
  [ "$dirty" != "0" ] && echo "WARN: working tree is dirty; commit or stash before splitting so every step is revertible"
  base=$(git rev-parse --abbrev-ref origin/HEAD 2>/dev/null | sed 's#origin/##')
  [ -n "$base" ] && [ "$branch" = "$base" ] && echo "WARN: you are on the default branch; work on a feature branch (e.g. refactor/split-<module>)"
else
  echo "ERROR: not a git repository"; missing+=("git-repo")
fi

echo "== claude code settings =="
for f in .claude/settings.json .claude/settings.local.json; do
  if [ -f "$f" ] && grep -q '"baseRef"' "$f" && grep -q '"head"' "$f"; then echo "worktree.baseRef=head found in $f"; found_base=1; fi
done
if [ "${found_base:-0}" != 1 ]; then
  echo "NOTE: set  {\"worktree\": {\"baseRef\": \"head\"}}  in .claude/settings.json so subagent worktrees branch from THIS branch, not the default branch."
fi

echo "== summary =="
if [ ${#missing[@]} -gt 0 ]; then
  echo "PREFLIGHT FAILED: install missing tools (re-run with --install) : ${missing[*]}"; exit 1
fi
echo "PREFLIGHT OK"
