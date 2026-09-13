#!/usr/bin/env bash
# The gate. Run after every extraction step. Exit 0 = safe to commit.
#
#   verify.sh --pkg <path/to/package_or_dir> [--snapshot before.json] [--strict-bodies] [--fast]
#
# Env overrides:
#   REFACTOR_TEST_CMD   default: "pytest -q -x --no-header -p no:cacheprovider"
#   REFACTOR_TYPE_CMD   default: mypy <pkg> if mypy present, else pyright <pkg>, else skipped
#   REFACTOR_MAX_LINES  default 500
#
# Output is kept short on purpose: agents read this. Full logs go to .refactor-gate.log
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
PKG=""; SNAP=""; STRICT=""; FAST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pkg) PKG=$2; shift 2;;
    --snapshot) SNAP=$2; shift 2;;
    --strict-bodies) STRICT="--strict"; shift;;
    --fast) FAST=1; shift;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done
[ -z "$PKG" ] && { echo "usage: verify.sh --pkg <path> [--snapshot before.json] [--strict-bodies] [--fast]"; exit 2; }
LOG=.refactor-gate.log; : > "$LOG"
fail=0; results=()

run() { # label cmd...
  local label=$1; shift
  echo "--- $label: $*" >> "$LOG"
  if "$@" >> "$LOG" 2>&1; then results+=("PASS $label"); else results+=("FAIL $label"); fail=1; fi
}

run "compile"       python3 -m compileall -q "$PKG"
run "ruff-lint"     ruff check "$PKG"
run "file-length"   python3 "$HERE/check_file_length.py" "$PKG"

# Types: ratcheted against .refactor/type-baseline.json. First run with a checker available
# records pre-existing errors from committed HEAD (never from the dirty tree); after that
# only NEW errors fail. Undefined names left by a move are exactly what this catches.
if [ -n "${REFACTOR_TYPE_CMD:-}" ] || command -v mypy >/dev/null 2>&1 || command -v pyright >/dev/null 2>&1; then
  run "types" python3 "$HERE/type_baseline.py" check --pkg "$PKG" --auto-baseline
  grep -q '^BASELINED types' "$LOG" && results+=("NOTE types: baseline created from HEAD; commit .refactor/type-baseline.json")
else results+=("SKIP types (no mypy/pyright; run preflight.sh --install)"); fi

if command -v lint-imports >/dev/null 2>&1 && { [ -f .importlinter ] || grep -q '\[tool.importlinter\]' pyproject.toml 2>/dev/null || grep -q '\[importlinter\]' setup.cfg 2>/dev/null; }; then
  run "import-contracts" lint-imports
else results+=("SKIP import-contracts (no config)"); fi

if [ -n "$SNAP" ]; then
  run "bodies-unchanged" python3 "$HERE/snapshot_bodies.py" compare "$SNAP" "$PKG" $STRICT
else results+=("SKIP bodies-unchanged (no --snapshot)"); fi

if [ $FAST -eq 0 ]; then
  run "tests" bash -c "${REFACTOR_TEST_CMD:-pytest -q -x --no-header -p no:cacheprovider}"
else results+=("SKIP tests (--fast)"); fi

echo "== gate results =="
printf '%s\n' "${results[@]}"
if [ $fail -ne 0 ]; then
  echo "== first failure detail (tail of $LOG) =="
  awk '/^--- /{block=$0; buf=""} {buf=buf"\n"$0} END{}' "$LOG" >/dev/null
  # print the section of the first failing label
  first=$(printf '%s\n' "${results[@]}" | grep '^FAIL' | head -1 | awk '{print $2}')
  awk -v lbl="--- $first:" 'index($0,lbl)==1{p=1;next} /^--- /{if(p)exit} p' "$LOG" | tail -60
  echo "GATE: FAIL"
  exit 1
fi
echo "GATE: PASS"
exit 0
