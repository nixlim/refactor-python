# Plan: from "split a module" to "decompose a codebase"

Date: 2026-09-18. Plugin baseline: 6131bca. Scope: this plugin only. The forge-plugin repository is the proving ground (appendix), not the subject.

## 1. Goal

The plugin exists so agents can work on a Python codebase in parallel, with low context cost, without freehand LLM edits. Today it does one thing well: relocate whole top-level symbols out of a god-module with a deterministic mover and an AST oracle. That leaves the three shapes that hold most oversized code untouched: big classes, giant functions, and big test classes.

The target is code quality, not a size ceiling. A project configures two numbers: a **ceiling** the guard enforces (a project may set 3,000) and a **target** the planner plans to (a project may set 1,000, the plugin default stays 500). Anything that lands between target and ceiling is reported as tracked debt with the reason, never presented as done.

## 2. Three tiers of guarantee

Every operation the plugin performs belongs to exactly one tier, and the report says which.

| Tier | Operation | Tool | What the oracle proves | Behaviour contract |
|---|---|---|---|---|
| 1 Relocation | move a symbol or a method, unchanged, somewhere else | rope (top level), LibCST (methods) | before and after are the same multiset of bodies, keys remapped by a manifest | oracle alone |
| 2 Declared rewrite | extract a statement range into a function; rename `self` to a typed parameter; replace a binding with a direct call | rope `ExtractMethod`, LibCST | before, transformed by the declared rule, equals after; nothing else changed | oracle plus tests |
| 3 Design change | introduce a state object; re-compose a class; regroup tests by scenario | agents under review | only the methods named in an allow-list changed; census and test-id checks hold | tests, two-model review, operator approval |

Tiers 1 and 2 are mechanical and refuse what they cannot prove. Tier 3 is where judgement lives, and it comes last so that its diff is small and reviewable after tiers 1 and 2 have already spread the code out.

Verified in this session on 6131bca: a method moved verbatim to a module function or a mixin keeps its AST hash; mypy treats a class-body binding `name = _mod.name` as a method with `self` untyped (no new errors), while a mixin produces new `attr-defined` errors; `patch.object(Class, "name")` works under both; unittest and pytest collect inherited test methods under the subclass with unchanged ids; rope extracts a statement range to a module function and infers its inputs and outputs.

## 3. Capabilities to build

### C1. Class inventory and census (`class_inventory.py`)

Method-level graph for one class: lines, decorators, `self`/`cls` attribute reads and writes, `self` calls, module globals referenced, nested defs, `nonlocal`, `super()`, `__class__`, name-mangled names; class-level facts: bases, metaclass, `__slots__`, class-body statements other than defs. Clustering reuses the label propagation in `inventory.py` over edges of shared attributes and `self` calls, optionally seeded with seam names. Repo-wide census of every way the outside world touches the class: `patch.object` including multi-line forms, string patches, plain assignments onto the class, unbound reads and calls, `vars`/`__dict__`/`inspect.getsource`/`__qualname__`. Each method gets a verdict: `ok`, `wrap` (decorator needs a wrapper in the function shape), `unsupported` with the reason.

Also a **function inventory** for giant functions: statement blocks bounded by blank lines and comments, per-block inputs and outputs, mid-block `return`/`yield`/`break`, nested defs and whether they close over mutable state. This is what makes extract-function plannable rather than freehand.

### C2. Manifest-driven oracle (`snapshot_bodies.py compare --manifest`)

One manifest per cluster lists moves and extracts. Moves remap keys and require exactly one surviving definition with the same hash. Extracts are checked structurally: the new function's body equals the removed statement range plus its return, and the source body equals the old body with that range replaced by the one call statement. Containers are checked, not waived: the class after, with moved methods and declared binding lines stripped and bases normalised to the declared list, hashes equal to the class before with the same methods stripped. `--allow-changed` stays for tier 3 only and must name methods, never a class.

### C3. Method mover (`move_methods.py`, LibCST)

Three destination shapes, chosen per class in the plan:

- **function**: module-level function, body and parameter names untouched, with a class-body binding kept (`name = _mod.name`, wrapped for `staticmethod`/`classmethod`/`property`) so patch sites and unbound calls keep working. Default for production classes, because it is neutral under a mypy ratchet.
- **mixin**: verbatim methods in a mixin class, the original class gains the base. Only for classes outside the type-gated package (test classes), because it preserves every test id and shard partition.
- **class**: a new sibling class in a new module, for regrouping test scenarios where nothing pins the ids.

Dry run by default, one manifest per run, stops at the first refusal, runs the C2 compare in memory before writing. Refuses unsupported hazards, destination name clashes, and unknown decorators. Import repair reuses the rules already in `rope_move.py`.

### C4. Extract-function (`extract_ranges.py`, rope)

Applies the extracts a plan names (function, statement range, new name, destination module or same module) through rope's `ExtractMethod` with `global_=True` when the target is a module function, then runs the C2 structural check. Refuses ranges rope refuses (mid-range returns, partial loops). Nested defs that do not use `nonlocal` are hoisted to module functions with explicit parameters as a second extract kind. The planner uses the C1 function inventory to propose ranges; the critic rejects extracts whose new function has more than a configured number of parameters, because that is a sign the range was wrong, not a reason to add a state object.

### C5. Test-id snapshot and compare (`collect_tests.py`)

Snapshot of all test ids from `unittest.defaultTestLoader` and, when present, `pytest --co`, plus per-shard membership for modules that define a shard suite. Compare modes: `identity` (mixin shape, pinned ids) and `mapping` (class shape, the manifest declares old id to new id, counts and mapping must match exactly). Refuses new support modules whose filename matches the discovery pattern, and mixins whose name matches the collector's class pattern.

### C6. Quality rules the planner must satisfy

Configurable, with defaults, in one place the SKILL documents:

- module target and ceiling (500 and 500 by default; a project raises them);
- function length target (150 lines): anything over is an extract candidate, and a module made of three 700-line functions is not an accepted plan;
- class size target (30 methods): anything over is a split candidate;
- cohesion: a cluster whose methods share no state and no calls is two clusters;
- test modules: one scenario family per module, helpers in a support module, ids preserved where pinned;
- every module between target and ceiling is listed in the report with the reason and a follow-up.

### C7. Agents, skill, workflow, docs

- `split-planner`: class mode and function mode, emitting manifests, with the C6 rules as its checklist.
- `plan-critic`: attacks manifests against C6, the census, and the refusal list.
- `extractor`: brief variants for `move_methods.py` and `extract_ranges.py`; bodies are still never edited by hand.
- `refactor-reviewer` and `gate-runner`: run C2 with the manifest and C5; read the class header diff against the manifest.
- New skill `decompose` covering class, function and test-class decomposition with the same phase structure as `split-module` (preflight, freeze including test ids, inventory, plan and critique, census repoint, sequential extraction per source file, wave close, finalize, Codex then Fable review). `split-module` stays as the tier-1 module case.
- `workflows/decompose.js`: a copy of `split-module.js` with manifest handling (a copy, because resume caching is a call-sequence prefix).
- Rule amendments stated explicitly: class headers change only as a manifest states; mixins only for test classes; "never add a base class" stays for production code; tier 2 and tier 3 operations are separate commits from tier 1 moves.
- `references/failure-modes.md` rows for each new refusal, README layout and use sections.

### C8. Public readiness

The plugin is already public and will be announced once it proves itself, so nothing project-specific may live in the scripts. Project-specific steps already enter through hooks (`REFACTOR_TEST_CMD`, `mintCmd`, focused tests, the rope-ignore file); the digest re-mint step gets the same treatment as a documented hook. Deliverables: a fixture repository under `skills/decompose/tests/fixture/` (a production class with static, classmethod, property, contextmanager, nested closures, one mangled method; a giant function with extractable ranges and one refused range; a test class with a shard `load_tests`, pinned ids and patch sites), a pytest suite that runs every capability and every refusal against it, CI running that suite, and a worked example in the README with before and after line counts.

## 4. Order

1. C1 and C2 (the guarantees; small).
2. C5 (small) and C3 (the one substantial script).
3. C4 with the function inventory half of C1.
4. C6 and C7 prose, skill, workflow.
5. C8 fixture, tests, CI, README. Dress rehearsal on the proving ground in dry run only.

Nothing here depends on the proving ground's reintegration bead.

## Appendix: the proving ground (forge-plugin at cc2cf98, target 1,000, ceiling 3,000)

Expected outcomes, to validate the plan rather than to schedule work:

- `app/_merge_engine.py` (10,302 code lines, 93 methods, 8 methods over 250 lines, largest 843): tier 1 function shape along seven seams into about nine modules; tier 2 extracts on the eight giant methods; then sub-seam modules near 1,000 lines with no function over 150. Tier 3 later, if wanted: typed seam functions replacing the bindings, and a state object for the 22 `nonlocal` closures, each a separate reviewed commit. The 9 class-patch sites in tests survive tier 1 unchanged.
- `engine/_engine.py` (3,574, 41 methods): tier 1 function shape, three or four modules.
- Test files: `test_cli_merge_integration.py` and `test_cli_merge_lifecycle.py` have pinned ids and a shard suite, so mixin shape at scenario granularity (about 600 to 1,000 lines each). `test_revision9_coordination.py`, `test_revision8_coordination.py`, `test_revision9_cli_surfaces.py`, `test_commit_guard.py` pin nothing, so class shape: new scenario modules with real class names, helpers in a support module, ids mapped.
- `fresh_evals.py`, `archive-run.py`, and the vendored `codex_orchestrator` trio: existing `split-module`, at the project's target.
- `commit-guard.sh`: not Python, out of scope.
