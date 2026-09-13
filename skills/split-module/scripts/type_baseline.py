#!/usr/bin/env python3
"""Type-check ratchet for repos that were never type-checked.

Pre-existing mypy/pyright errors are recorded once (from a COMMITTED ref, never from a
dirty working tree) and grandfathered. Afterwards `check` fails only on errors that are
new relative to that baseline. Errors are keyed by (error code, message) rather than by
file:line, so an error that merely moved with its code during a split is recognized as
relocated, not new.

    type_baseline.py snapshot --pkg app/core [--ref HEAD] [--out .refactor/type-baseline.json]
    type_baseline.py check    --pkg app/core [--baseline .refactor/type-baseline.json] [--update]
    type_baseline.py status

Tool selection: REFACTOR_TYPE_CMD (full command, run with cwd = repo/worktree root),
else mypy if present, else pyright. Output parsing handles both.

Exit codes: 0 ok, 1 new type errors (or tool failure), 2 usage.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

DEFAULT_BASELINE = ".refactor/type-baseline.json"
MYPY_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+)(?::\d+)?: (?P<kind>error|note): (?P<msg>.*?)(?:\s+\[(?P<code>[\w-]+)\])?$")


def type_cmd(pkg: str) -> list[str] | str:
    env = os.environ.get("REFACTOR_TYPE_CMD")
    if env:
        return env  # shell string
    if shutil.which("mypy"):
        return ["mypy", "--no-error-summary", "--no-color-output", "--show-error-codes", "--hide-error-context", pkg]
    if shutil.which("pyright"):
        return ["pyright", "--outputjson", pkg]
    raise SystemExit("no type checker: install mypy or pyright (preflight.sh --install)")


def run_checker(pkg: str, cwd: str) -> tuple[list[dict], str]:
    cmd = type_cmd(pkg)
    proc = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True, text=True)
    out = proc.stdout
    errors: list[dict] = []
    if out.lstrip().startswith("{"):  # pyright json
        try:
            data = json.loads(out)
            for d in data.get("generalDiagnostics", []):
                if d.get("severity") != "error":
                    continue
                errors.append({"file": os.path.relpath(d.get("file", ""), cwd), "line": d.get("range", {}).get("start", {}).get("line", 0) + 1,
                               "code": d.get("rule") or "pyright", "msg": _norm(d.get("message", ""))})
            return errors, out
        except json.JSONDecodeError:
            pass
    for line in out.splitlines():
        m = MYPY_RE.match(line)
        if m and m.group("kind") == "error":
            errors.append({"file": m.group("file"), "line": int(m.group("line")), "code": m.group("code") or "mypy", "msg": _norm(m.group("msg"))})
    if proc.returncode not in (0, 1) and not errors:  # 1 = errors found; anything else is a tool failure
        print(f"type checker failed (rc={proc.returncode}):\n{(proc.stderr or out)[-2000:]}", file=sys.stderr)
        raise SystemExit(1)
    return errors, out


def _norm(msg: str) -> str:
    # strip volatile bits: absolute paths, quoted line-specific temporaries are rare; collapse whitespace
    return re.sub(r"\s+", " ", msg.strip())


def key(e: dict) -> str:
    return f"{e['code']}::{e['msg']}"


def snapshot_at_ref(pkg: str, ref: str) -> list[dict]:
    """Run the checker in a temporary worktree of `ref` so a dirty tree never leaks in."""
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
    tmp = tempfile.mkdtemp(prefix="typebase-")
    try:
        subprocess.run(["git", "worktree", "add", "--detach", "-q", tmp, ref], cwd=root, check=True, capture_output=True)
        errors, _ = run_checker(pkg, tmp)
        return errors
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", tmp], cwd=root, capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def write_baseline(path: str, pkg: str, ref: str, errors: list[dict]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    counts = Counter(key(e) for e in errors)
    data = {"pkg": pkg, "ref": ref, "tool": (os.environ.get("REFACTOR_TYPE_CMD") or ("mypy" if shutil.which("mypy") else "pyright")),
            "total": len(errors), "errors": dict(sorted(counts.items())),
            "examples": {key(e): f"{e['file']}:{e['line']}" for e in errors}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--pkg", required=True); s.add_argument("--ref", default="HEAD"); s.add_argument("--out", default=DEFAULT_BASELINE)
    c = sub.add_parser("check"); c.add_argument("--pkg", required=True); c.add_argument("--baseline", default=DEFAULT_BASELINE)
    c.add_argument("--update", action="store_true", help="ratchet the baseline down when errors disappeared (never up)")
    c.add_argument("--auto-baseline", action="store_true", help="if no baseline exists, create one from HEAD first (used by verify.sh)")
    sub.add_parser("status")
    args = ap.parse_args()

    if args.cmd == "status":
        tool = os.environ.get("REFACTOR_TYPE_CMD") or ("mypy" if shutil.which("mypy") else "pyright" if shutil.which("pyright") else "none")
        has = os.path.exists(DEFAULT_BASELINE)
        info = json.load(open(DEFAULT_BASELINE)) if has else {}
        print(f"type checker: {tool}; baseline: {'yes' if has else 'no'}" + (f" ({info.get('total')} grandfathered errors, ref {str(info.get('ref'))[:12]}, pkg {info.get('pkg')})" if has else ""))
        return 0

    if args.cmd == "snapshot":
        ref = subprocess.run(["git", "rev-parse", args.ref], capture_output=True, text=True, check=True).stdout.strip()
        errors = snapshot_at_ref(args.pkg, ref)
        write_baseline(args.out, args.pkg, ref, errors)
        print(f"type baseline: {len(errors)} pre-existing error(s) recorded from {ref[:12]} -> {args.out}")
        return 0

    # check
    if not os.path.exists(args.baseline):
        if not args.auto_baseline:
            print(f"no type baseline at {args.baseline}; run: type_baseline.py snapshot --pkg {args.pkg}", file=sys.stderr)
            return 1
        ref = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        errors = snapshot_at_ref(args.pkg, ref)
        write_baseline(args.baseline, args.pkg, ref, errors)
        print(f"BASELINED types: {len(errors)} pre-existing error(s) recorded from committed HEAD {ref[:12]}; the gate now fails only on NEW errors. Commit {args.baseline}.")
    with open(args.baseline, encoding="utf-8") as f:
        base = json.load(f)
    allowed = Counter(base["errors"])
    now, raw = run_checker(args.pkg, os.getcwd())
    current = Counter(key(e) for e in now)
    new = current - allowed        # multiset difference: more occurrences than baselined
    gone = allowed - current
    if new:
        print(f"NEW type errors vs baseline ({sum(new.values())}):")
        by_key = {}
        for e in now:
            by_key.setdefault(key(e), []).append(f"{e['file']}:{e['line']}")
        for k, n in sorted(new.items()):
            code, msg = k.split("::", 1)
            print(f"  [{code}] {msg}  x{n}  at {', '.join(by_key.get(k, [])[:5])}")
        print("These are not in the grandfathered set: typically an undefined name left by a move, a dropped import, or a self-import.")
        return 1
    if gone and args.update:
        base["errors"] = dict(sorted((allowed - gone).items())); base["total"] = sum(base["errors"].values())
        with open(args.baseline, "w", encoding="utf-8") as f:
            json.dump(base, f, indent=1)
        print(f"type baseline ratcheted down by {sum(gone.values())} (now {base['total']})")
    print(f"types ok: {len(now)} error(s), all grandfathered ({sum(gone.values())} fixed since baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
