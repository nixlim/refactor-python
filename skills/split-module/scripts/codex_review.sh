#!/usr/bin/env bash
# Headless second-opinion review by OpenAI Codex (default: gpt-5.6-sol, reasoning effort ultra).
# Operational details follow the codex-orchestrator plugin (alexzh3/codex-orchestrator):
# locate the IDE-bundled binary if none on PATH, read-only sandbox, approval_policy=never,
# capture the final message with -o, keep the session (no --ephemeral) so it can be resumed
# for the consensus round.
#
#   codex_review.sh --base <baseline-ref> [--head HEAD] [--plan .refactor/plan-x.md]
#                   [--out .refactor/codex-review.md] [--model gpt-5.6-sol] [--effort ultra]
#                   [--native]        # use `codex exec review --base` instead of the structured prompt
#                   [--followup <thread-id> "<finding text>"]   # consensus round: ask Codex to respond to a finding
#
# Exit 0 = review captured (verdict is in the output file; read it). Exit 1 = codex failed.
set -u
BASE=""; HEAD=HEAD; PLAN=""; OUT=.refactor/codex-review.md; MODEL=${CODEX_REVIEW_MODEL:-gpt-5.6-sol}
EFFORT=${CODEX_REVIEW_EFFORT:-ultra}; NATIVE=0; FOLLOW=""; FOLLOW_TEXT=""
while [ $# -gt 0 ]; do case "$1" in
  --base) BASE=$2; shift 2;; --head) HEAD=$2; shift 2;; --plan) PLAN=$2; shift 2;; --out) OUT=$2; shift 2;;
  --model) MODEL=$2; shift 2;; --effort) EFFORT=$2; shift 2;; --native) NATIVE=1; shift;;
  --followup) FOLLOW=$2; FOLLOW_TEXT=$3; shift 3;; *) echo "unknown arg $1" >&2; exit 2;; esac; done

CODEX=$(command -v codex 2>/dev/null)
[ -z "$CODEX" ] && CODEX=$(find ~/.cursor/extensions ~/.vscode/extensions ~/.vscode-server/extensions -maxdepth 4 -name codex -type f 2>/dev/null | head -1)
[ -z "$CODEX" ] && { echo "codex binary not found (PATH or IDE extension dirs). Install Codex CLI or the IDE extension." >&2; exit 1; }
mkdir -p "$(dirname "$OUT")"; LOG=${OUT%.md}.jsonl

run_codex() { # $@ = extra args; prompt on stdin
  "$CODEX" exec -s read-only -c approval_policy=never -m "$MODEL" -c "model_reasoning_effort=\"$EFFORT\"" \
     --skip-git-repo-check -o "$OUT" --json "$@" > "$LOG" 2>&1; }

if [ -n "$FOLLOW" ]; then
  printf '%s\n' "A reviewer disagrees with or wants evidence for one of your findings. Respond with evidence from the repository (file:line, commands you ran), then state AGREE, DISAGREE, or RETRACT for the finding, and why. Finding under discussion:
$FOLLOW_TEXT" | "$CODEX" exec resume "$FOLLOW" -c 'sandbox_mode="read-only"' -c approval_policy=never -m "$MODEL" -c "model_reasoning_effort=\"$EFFORT\"" -o "$OUT" --json > "$LOG" 2>&1
  rc=$?; [ $rc -ne 0 ] && { tail -20 "$LOG" >&2; exit 1; }; cat "$OUT"; exit 0
fi

[ -z "$BASE" ] && { echo "--base <baseline-ref> is required" >&2; exit 2; }
FILES=$(git diff --name-only "$BASE" "$HEAD" | wc -l | tr -d ' ')
PLANTXT=""; [ -n "$PLAN" ] && [ -f "$PLAN" ] && PLANTXT=$(cat "$PLAN")

if [ $NATIVE -eq 1 ]; then
  "$CODEX" exec review --base "$BASE" -m "$MODEL" -c "model_reasoning_effort=\"$EFFORT\"" -o "$OUT" --json > "$LOG" 2>&1
else
  PROMPT=$(cat <<PROMPT
You are an independent reviewer of a mechanical refactor: a large source file was split into
smaller files/modules. The claim is that NO behavior changed. Your job is to find evidence
against that claim. Compare $BASE..$HEAD ($FILES files changed). Run git and read code yourself;
do not trust narration.

Look specifically for:
1. Dropped or duplicated declarations (a function/class/type present before, absent or doubled after).
2. Edited bodies disguised as moves (compare the moved definition text before and after).
3. Module/package-level state that now exists in two places, or whose initialization order changed.
4. Import cycles hidden by lazy/local imports, or new abstractions (interfaces, base classes,
   registries, 'common' packages) introduced to make a move compile.
5. Public API breaks: symbols previously importable from the original path that no longer are.
6. Anything in the diff that is not a move, an import change, a re-export, or a type-only import.
$( [ -n "$PLANTXT" ] && printf 'The split plan the agents followed is below; flag deviations from it.\n---PLAN---\n%s\n---END PLAN---\n' "$PLANTXT" )
Output EXACTLY this format and nothing after it:
VERDICT: APPROVE | REJECT
CONFIDENCE: high | medium | low
BLOCKING:
- <file>:<line> — <what changed and why it alters behavior or API> (evidence: <command or diff hunk>)
ADVISORY:
- <finding>
CHECKED:
- <one line per check 1-6: what you ran and what you saw>
PROMPT
)
  printf '%s\n' "$PROMPT" | run_codex
fi
rc=$?
if [ $rc -ne 0 ] && grep -qiE 'ultra|reasoning_effort' "$LOG"; then
  echo "note: effort '$EFFORT' rejected by this backend; retrying with xhigh" >&2
  EFFORT=xhigh; printf '%s\n' "${PROMPT:-}" | run_codex; rc=$?
fi
THREAD=$(grep -o '"thread_id": *"[^"]*"' "$LOG" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
if [ $rc -ne 0 ] || [ ! -s "$OUT" ]; then echo "codex review failed (rc=$rc); tail of $LOG:" >&2; tail -20 "$LOG" >&2; exit 1; fi
{ echo; echo "---"; echo "codex_thread: ${THREAD:-unknown}"; echo "model: $MODEL  effort: $EFFORT"; } >> "$OUT"
echo "codex review written to $OUT (thread ${THREAD:-unknown}); verdict line:"; grep -m1 '^VERDICT' "$OUT" || echo "(no VERDICT line; read $OUT)"
