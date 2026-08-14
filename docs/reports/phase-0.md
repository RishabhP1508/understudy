## Phase 0 Verification Report

Status: COMPLETE

Loop summary: 2 rounds.

Round 1 produced the whole skeleton and every local check came back green. Reading the diff turned up
two real problems. First, `pyproject.toml` had gained a `[tool.ruff.lint.flake8-bugbear]`
`extend-immutable-calls` exemption so that typer's `param: T = typer.Option(...)` defaults would stop
tripping B008. That loosens the checker to fit the code, which is the one direction CLAUDE.md's
anti-gaming rule forbids, and typer's current `Annotated` form avoids the rule outright. Second,
invariants 3, 4, and 5 decided whether to skip with `try: import ... except ImportError:
pytest.skip(...)`. That conflates "not written yet" with "written but broken": if in Phase 6 a
guarded module existed but had a bad import inside it, the invariant would skip with a reason
claiming it arrives in Phase 2, and the constraint would sit dormant for the rest of the build. Since
this file freezes when Phase 0 closes, that was the only chance to fix it.

Round 2 rewrote the CLI options with `typing.Annotated` and deleted the exemption, changed the three
gates to skip on file absence and then import unguarded, and shrank `load_policy` to one line. I then
re-ran every check and the entire adversarial probe against the modified test file.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] test suite exits 0 — ran: `.venv/Scripts/python.exe -m pytest -q` — got: `sssss`, 5 skipped,
  `pytest_exit=0` — expected: exit 0
- [x] exactly five test functions in tests/test_constraints.py — ran: `grep -c "^def test_"
  tests/test_constraints.py` — got: `5` — expected: 5
- [x] no public helper masquerading as a test — ran: `grep "^def " tests/test_constraints.py | grep -v
  "^def test_" | grep -v "^def _"` — got: no output — expected: no output
- [x] each of the five names its invariant in a docstring — ran: read the file — got: five docstrings
  reading "Invariant 1:" through "Invariant 5:" — expected: one per test
- [x] every skipped invariant prints a reason naming the phase that enables it — ran:
  `.venv/Scripts/python.exe -m pytest -q` (`addopts = "-q -rs"` makes `-rs` permanent) — got: five
  SKIPPED lines, each naming Phase 2 — expected: five non-silent skips
- [x] lint clean — ran: `.venv/Scripts/python.exe -m ruff check .` — got: `All checks passed!`,
  `ruff_exit=0` — expected: exit 0
- [x] types clean — ran: `.venv/Scripts/python.exe -m mypy src/` — got: `Success: no issues found in
  13 source files`, `mypy_exit=0` — expected: exit 0
- [x] no ruff exemption remains — ran: `grep -n "flake8-bugbear" pyproject.toml` — got: no match —
  expected: no match
- [x] directory tree matches the CLAUDE.md layout — ran: `find . -type d` (see listing below) — got:
  every layout directory present, nothing extra — expected: exact match on the Phase 0 subset
- [x] CLI entry point works — ran: `.venv/Scripts/python.exe -m understudy.cli --help` — got: usage
  block listing `discover`, `replay`, `operator`, `catalog`, exit 0 — expected: all four subcommands
- [x] all four subcommands exit 2 — ran each in turn — got: `discover_exit=2`, `replay_exit=2`,
  `operator_exit=2`, `catalog_exit=2`, each printing `not implemented` — expected: exit 2
- [x] `discover` takes goal and target as separate inputs, target documented as defaulting to the
  policy entry_point — ran: `COLUMNS=200 ... discover --help` — got: `--goal <str> natural-language
  goal for the agent [required]` and `--target <str> app id, URL, or entry point; defaults to the
  policy entry_point` — expected: both flags, `entry_point` visible and unwrapped
- [x] the target default resolves and is echoed — ran: `... discover --goal "x"` — got: `target:
  http://127.0.0.1:8000/` then `not implemented` — expected: the policy entry_point echoed
- [x] an explicit target overrides it — ran: `... discover --goal g --target http://example.test/x` —
  got: `target: http://example.test/x` — expected: the supplied value
- [x] no file outside the declared structure — ran: `find . -type f` minus gitignored paths — got: 22
  files, all in the layout — expected: no strays
- [x] .gitignore covers .env — ran: `grep -n "^\.env$" .gitignore` — got: `2:.env` — expected: a match
- [x] no hardcoded key, token, password, or connection string in any created file — ran: a case
  insensitive regex sweep for api_key/secret/token/password/bearer/`AIza…`/`sk-…`/db connection URIs
  across pyproject.toml, .env.example, policies/, src/, tests/, .github/, docs/, ARCHITECTURE.md —
  got: only the env var NAME `GEMINI_API_KEY`, the field name `gemini_api_key`, docstrings, and the
  two deliberate fake test sentinels — expected: no secret values
- [x] ARCHITECTURE.md exists and records every CLAUDE.md constraint as a decision with a why — ran:
  `grep -cE "^[0-9]+\. "` and `grep -ciE "why:"` plus a 21-keyword coverage sweep — got: 24
  decisions, 24 why lines, all 21 keywords covered, 0 em dashes — expected: full coverage
- [x] docs/reports/phase-0.md saved — this file

Extra gate I imposed on myself, because five skipping tests prove nothing:

- [x] all five invariants actually bite, and do not false-positive — ran: 15 scenarios against an
  adversarial copy of the tree in the scratchpad, using the real test file (details below)

### Directory listing checked against the layout

    $ find . -type d   (excluding .venv, .claude, caches, egg-info)
    .              ./artifacts   ./docs        ./docs/adr    ./docs/reports  ./evidence
    ./fixtures     ./fixtures/legacy_bank      ./overlays    ./policies      ./src
    ./src/understudy            ./src/understudy/agent       ./src/understudy/catalog
    ./src/understudy/escalation ./src/understudy/evidence    ./src/understudy/llm
    ./src/understudy/models     ./src/understudy/record      ./src/understudy/replay
    ./src/understudy/safety     ./src/understudy/surface     ./tests
    ./.github      ./.github/workflows

Every directory in the CLAUDE.md layout is present and nothing else is. The layout FILES tagged for
later phases are deliberately absent: models/*.py, surface/*.py, llm/*.py, agent/*.py, record/*.py,
replay/*.py, safety/*.py, evidence/logger.py, escalation/*.py, catalog/server.py (all [P2] or later),
policies/legacy_bank_tenant_b.yaml and overlays/tenant_b.json ([P12]), README.md and REPORT.md
([P14]). Each package directory holds an empty `__init__.py` so the tree is importable now.

### Invariants

    $ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -q
    sssss                                                                    [100%]
    =========================== short test summary info ===========================
    SKIPPED [1] tests\test_constraints.py:256: src/understudy/replay/ arrives in Phase 2; invariant 1 goes live then
    SKIPPED [1] tests\test_constraints.py:268: src/understudy/safety/policy.py (PolicyGate.dispatch) arrives in Phase 2; invariant 2 goes live then
    SKIPPED [1] tests\test_constraints.py:283: understudy.safety.redact.Redactor arrives in Phase 2 (stub) and Phase 5 (full); invariant 3 goes live then
    SKIPPED [1] tests\test_constraints.py:314: understudy.models.artifact.Capability arrives in Phase 2; invariant 4 then
    SKIPPED [1] tests\test_constraints.py:342: understudy.agent.tools.FINISH_TOOL arrives in Phase 2; invariant 5 then
    exit 0

All five skip at Phase 0 because the code they guard does not exist yet, which is what CLAUDE.md
predicts ("All five go live in Phase 2 against correctly-shaped stubs"). No skip is silent.

Because a skipping test is indistinguishable from a broken one, I copied the real test file into the
scratchpad next to a deliberately non-compliant tree and confirmed each invariant fires. 15 scenarios:

Violations correctly caught:
1. replay/engine.py reaching an LLM through two hops of relative imports
   (`replay.engine` to `record.canonicalize` to `llm.gemini` to `google.genai`). Caught, and it named
   all three banned modules, which confirms the relative-import resolution and the transitive walk.
2. A rogue `surface.act(...)` in surface/web.py outside the gate. Caught: reported
   `(None, 'navigate')` as an extra call site.
3, 4, 5. A Redactor that base64-encodes the whole payload, run at all three byte alignments
   (`PROBE_OFFSET` 0, 1, 2) so the sentinel does not start on a 3-byte boundary. Caught in all three.
6. A Capability schema with a banned `messages` property and no `provenance.transcript_hash`. Caught.
7. A FINISH_TOOL with `required: []`. Caught.
9. An artifact on disk carrying a `messages` key. Caught, naming the file.
10. An artifact whose `provenance.transcript_hash` was `NOT-A-DIGEST`. Caught.
11. A loop.py that returns `{"ok": True}` on the finish branch without verifying. Caught:
    "finish handling in loop.py never calls verify_checkpoint".
14. All five violated at once after the round 2 edit: 5 failed, 0 skipped.
15. The round 2 fix itself: redact.py present but importing a module that does not exist. Now fails
    with `ModuleNotFoundError`. Before the fix this skipped with a reason claiming Phase 2.

No false positives:
8. A compliant tree (real redactor, transcript_hash present, checkpoint required, verify before
   return, a clean on-disk artifact with a real sha256): 5 passed.
12. Finish handled through `match tool_name: case "finish":` with verify before return: passed, so
    the check survives the match statement shape a later phase may use.
13. The same clean tree re-run against the round 2 test file: 5 passed.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

No model ran this phase, so there are no discovery steps, rejected turns, replay stability, or
locator ranks to report. What exists: 2 builder rounds; 22 files; 13 source files type-checked; 5
invariants, all 5 skipped by design; 15 adversarial probe scenarios, all behaving as intended.

### How the core piece works  (plain English)

The load-bearing piece of Phase 0 is tests/test_constraints.py, which turns five prose rules into
five executable ones. Rather than grep for forbidden text, it parses the source. Invariant 1 builds a
module graph from the AST of everything under replay/, resolving relative imports against each
module's own package, and walks it breadth-first to see whether any path reaches an LLM client or a
provider SDK. Invariant 2 visits every call node in src/ while tracking the enclosing class and
function, collects the location of every `.act(...)` call, and asserts that set is exactly
`{("PolicyGate", "dispatch")}`. Invariant 3 pushes two fake secrets through the single serialization
path and looks for them in the output as plain text, as percent-encoding, and as base64 at all three
byte alignments, since a secret buried in an encoded blob need not begin on a 3-byte boundary.
Invariant 4 reads the Capability JSON schema, and any artifact already on disk, and asserts no key is
named messages, transcript, completion, choices, or content, while requiring
`provenance.transcript_hash` to exist and to be a hex digest. Invariant 5 checks the finish tool
demands a checkpoint, then finds the `"finish"` literal in loop.py, takes the enclosing branch, and
asserts a `verify_checkpoint` call happens there with no `return` before it. Until the guarded file
exists each test skips with a reason naming the phase that supplies it, and it skips only on a
missing file, so a module that exists but fails to import errors out instead of going quiet.

### Decisions logged

- docs/adr/0001-python-311-pin.md — develop and run CI on 3.11 rather than the machine default 3.14,
  because pydantic-core, Playwright, and google-genai lag new interpreter releases on wheels.

### Caveats / not done

- CI has never actually run. The workflow file is written, but nothing is committed or pushed, so
  "CI is green from a clean checkout" is unproven. Locally I ran the same three commands the workflow
  runs and all three pass. The remaining risk is Linux plus a fresh `pip install -e ".[dev]"`, which
  cannot be reproduced here.
- Six directories are empty: artifacts/, evidence/, overlays/, fixtures/legacy_bank/, docs/reports/
  before this file landed, and .github is the only dotdir with content. Git does not track empty
  directories, so they will not survive a commit. They are recreated by the phase that writes into
  them, so nothing needs adding, but do not be surprised when they vanish from a fresh clone.
- `config.Settings` and `load_settings` have no caller yet. Nothing reads `GEMINI_API_KEY` until
  Phase 2. They exist because CLAUDE.md specifies config.py loads settings from env plus a policy
  path; ponytail's first rung would otherwise delete them.
- No .env loading. `config.load_settings` reads `os.environ` only, so the existing .env is not picked
  up automatically yet. Phase 2 needs that and can add it in about five lines.
- Bare `replay` with no arguments exits 2 through typer's usage error ("Missing option
  '--artifact'"), not through the stub path. With arguments (`replay --artifact a --params '{}'`) it
  prints `not implemented` and exits 2. Both are exit 2; the flags stay required because they are
  genuinely required inputs from Phase 9 onward.
- ARCHITECTURE.md is 130 lines and 1247 words, which is right at two pages rather than comfortably
  under. Trimming further would have meant deleting a decision's "why", and the Definition of Done
  wants every constraint to carry one. Flagging the measurement instead of quietly claiming the
  limit.
- tests/test_constraints.py was edited in round 2, after being created in round 1. The builder
  correctly flagged that its own instructions say never to touch that file. I directed the edit and
  stand behind it: the file is tagged [P0], it freezes when this phase closes rather than mid-build,
  and the change strictly tightens the gate. From Phase 1 onward it is untouchable.
- Three findings from /ponytail-review were declined on purpose, all inside
  tests/test_constraints.py: two near-identical recursive dict walkers, three methods where a
  `visit_AsyncFunctionDef = visit_FunctionDef` alias would do, and an `isinstance(output, bytes)`
  branch for a `dumps()` contracted to return `str`. About 15 lines. That file becomes immutable and
  I had just proved the current version behaves correctly in 15 scenarios; churning it for line count
  in the one place where a subtle mistake silently disables a graded invariant is a bad trade. The
  two findings outside it (the bugbear exemption, and `load_policy`) were both applied.
- .gitignore has no trailing newline. Harmless, and it was a pre-existing file I was told not to
  rewrite.
