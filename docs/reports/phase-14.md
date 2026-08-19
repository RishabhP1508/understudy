## Phase 14 Verification Report
Status: COMPLETE

Loop summary: 3 rounds. Round 1 produced both documents and applied the four repo-audit code
changes; all of Part A was correct first time, but REPORT.md came in at 4041 words against a
three-page limit, asserted a factually false claim about the tenant B locator descriptors that
contradicted its own section 3, left two of the brief's defence-list items undefended, and dated
the inert route check as "five phases" where the primary source says Phase 5 to Phase 7. Round 2
fixed all four. The main session then corrected one misattributed file citation directly. Round 3
converted four semicolon field dumps to lists, named the audit as the finder of the deleted dead
code, and fixed the `--params` exit code caveat below. Round 3 also carried a request to change
`_loaded_urls`, which I withdrew after verifying my own caveat had misread the code; see Caveats.

Delegation: the builder wrote README.md and REPORT.md and applied the five Part A code changes.
The main session ran the ponytail debt harvest and the whole-repo audit, produced the fact sheet of
verified measurements the builder wrote from, ran a live discovery run, ran the full escalation
demo end to end, ran the CI-shaped offline gate, and found the four defects sent back in round 2.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] **Seven headings, in order, exact spelling**. Ran: `grep -n '^## ' REPORT.md`. Got:
  `3:## Architecture`, `46:## Artifact schema`, `73:## Determinism & error handling`,
  `105:## Heterogeneity & multi-tenant`, `133:## Escalation & handoff`, `158:## Safety`,
  `182:## Cuts`. Expected: exactly those seven, that order, that spelling.

- [x] **REPORT.md under three pages**. Ran:
  a word count excluding markdown bullet markers. Got: **1891 words**, 2.91 pages at 650 words per
  page. Expected: under three pages. It was 4041 words in round 1 and was compressed, not trimmed:
  all seven headings, the eight defence items, the five measured defects, the two heterogeneity
  limits, the escalation-first framing, the safety admission, and every part of section 7 survive.
  Note on the metric: a raw `len(text.split())` counts each `- ` bullet marker as a word, so after
  round 3 converted four passages to lists it read 1924 against 1891 actual words. I had been using
  the raw count as the gate, which pushed the builder into dropping articles to hit a number that
  was an artifact. Those were restored.

- [x] **No em dash or en dash in either deliverable**. Ran: `grep -c '' README.md REPORT.md` and
  `grep -c '–' README.md REPORT.md`. Got: `README.md:0 REPORT.md:0` for both. Expected: 0.

- [x] **Every CLI command reconciled against the code**. Ran:
  `python -m understudy.cli --help` plus `--help` on each subcommand. Got: eight commands,
  `discover replay record operator catalog approve fingerprint drift`, all eight documented in
  README.md with a purpose and a worked example. No command exists that is undocumented.
  Separately confirmed there is no `understudy` console script (`which understudy` fails,
  pyproject.toml declares no `[project.scripts]`), and README.md states that plainly and documents
  the `python -m understudy.cli` form instead.

- [x] **No-live-services path, key cleared**. Ran:
  `GEMINI_API_KEY= python -m understudy.cli replay --artifact <balance v3> --params '{"password": "demo-pass-1", "member_id": 12345}'`. Got: `{"kind": "success", "outputs": {"savings_balance": "$1,204.55"}, "steps_run": 7, "duration_ms": 2170.9}`,
  exit 0. And with `member_id: 99999`. Got: `{"kind": "business_outcome", "code": "member_not_found", "message": "No member found for the given id.", "observed": "No member matches that search."}`,
  exit 0. Expected: both work with no key, and the not-found case is an outcome at exit 0, not a
  failure.

- [x] **CI-shaped gate: the exact commands CI runs, no key, no fixture, no browser**. Fixture
  process killed first, verified down (`curl` returned 000). Ran
  `GEMINI_API_KEY= python -m ruff check .`. Got: `All checks passed!`.
  `GEMINI_API_KEY= python -m mypy src/`. Got: `Success: no issues found in 41 source files`.
  `GEMINI_API_KEY= python -m pytest`. Got: `301 passed, 29 skipped in 31.06s`, zero failures, and
  every skip names the reason and the command to fix it. Expected: green with skips only, which is
  what a CI run will produce.

- [x] **Full suite with the fixture running**. Ran: `python -m pytest`. Got:
  `330 passed in 136.79s (0:02:16)`, zero skips. Expected: the pre-phase baseline of 328 plus one
  test for the drift fix and one for the `--params` exit code.

- [x] **PowerShell `--params` form executed as written**. Ran the README's exact block, backtick
  continuations included, through the PowerShell tool. Got:
  `{"kind": "success", "outputs": {"savings_balance": "$1,204.55"}, "steps_run": 7}`. Then ran the
  bash form in PowerShell to confirm the warning is warranted. Got:
  `JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)`,
  exit 1. Expected: the escaped form works, the unescaped form fails, so the dual-form
  documentation is load-bearing rather than decorative.

- [x] **Escalation demo (README step 4) end to end**. Started the operator console, ran the
  blocking replay of the draft subaccount capability with no API key. Got: the run blocked at step
  9, raised intervention `escbc057ec28d` with reason `risky_action_requires_approval`; the console
  rendered `PENDING HANDOFF (held by runner)`, and the detail page carried the capability, the goal,
  the step, what it tried, what it observed, a masked screenshot, redacted context, and the control
  history. `POST /intervention/escbc057ec28d/approve` returned 303, the blocked replay resumed on
  its own and finished `{"kind": "success", "steps_run": 10, "duration_ms": 55282.0}`, exit 0. The
  record shows `approval_consumed: true` and the custody chain
  `automation -> pending_handoff (runner) -> automation (operator: approved)`.

- [x] **The drift defect is fixed and the fix is real**. Ran:
  `python -m understudy.cli drift --evidence-dir evidence`. Got: 14 runs, where before the fix the
  same command reported 9 and silently omitted both cross-tenant runs. `cross-tenant/tenant-b` now
  reports `step 0` and `step 8` at `recorded_rank=1 actual_rank=5 strategy=role_ordinal
  drift=rank_regressed+name_no_longer_matched`, with `rank distribution: rank 1: 9, rank 5: 2`.

- [x] **The four audit deletions landed**. Ran greps for each: `AmbiguousTarget|TargetNotFound`
  returns nothing in `src/ tests/ fixtures/`; `redact_model` returns only
  `_redact_model_instance`; `max_action_retries` returns nothing in `src/ tests/ policies/`;
  `scratchpad_fixture.log` no longer exists.

- [x] **Repo state is clean and produced evidence is intact**. `artifacts/` holds the same 5 files
  as before the phase, `evidence/` holds the same 13 entries. Verified by content, not timestamp,
  that `evidence/discovery/run.jsonl` still carries `run_id b2405e162ba4`, `model
  gemini-3.6-flash`, 22,422 total tokens and 8 steps executed. Two empty `evidence/interventions/`
  directories created as side effects of verification runs (one by the builder, one by me) were
  found and removed.

### Human-review items  (the user confirms these)

- [ ] **Read README.md and REPORT.md end to end and confirm you can defend every sentence.** This
  is the one item no command can settle, and CLAUDE.md assigns it to you explicitly: you have to
  defend this in an interview.
- [ ] **The escalation handoff's genuinely manual half.** I verified the approval path
  programmatically, which is what the console's Approve button posts. The "take control, drive the
  browser by hand, hand it back" path is per ADR 0015 decision 80 only producible by a person; the
  curated proof of it is `evidence/escalation/escc59ee3e456.json` with its 19 recorded human
  actions. Check: open `http://127.0.0.1:8765/` during a blocked run and click through it yourself.
- [ ] **Everything that depends on a commit or a push.** Staging, committing, pushing, and the
  GitHub Actions run are yours by hand. I ran CI's exact commands locally instead; see the
  CI-shaped gate above.

### Invariants

    $ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -v
    platform win32 -- Python 3.11.15, pytest-9.1.1, pluggy-1.6.0
    collected 5 items
    tests\test_constraints.py .....                                          [100%]
    ============================== 5 passed in 0.41s ==============================

All five pass, none skipped. `tests/test_constraints.py` was not edited this phase.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

**A live discovery run of my own**, because the builder documented `discover` without executing it.
Ran the README's exact command with evidence directed outside the repo:

    status: goal_verified
    rounds: 8
    steps executed: 8
    rejected turns: 0
    outputs: {"savings_balance": "$1,204.55"}
    usage (this run): {"prompt_tokens": 21257, "completion_tokens": 270, "total_tokens": 21527}
    artifact written: artifacts\look-up-member-...-savings-balance.v4.json

That independently reproduces the shipped run's shape: 8 turns, 0 rejected turns, the same
extracted value. Token count differs run to run (21,527 here against the shipped run's 22,422), and
the model was `gemini-3.1-flash-lite` here against the shipped run's `gemini-3.6-flash`.

**Discovery against replay**, the measurement REPORT section 1 cites: 22,422 model tokens and 98.0
seconds to discover, 0 model tokens and 2.2 seconds to replay. Measured again this phase: 2.17s
(bash) and 2.16s (PowerShell).

**Locator rank distribution across all 14 evidence runs.** Every replay of the balance capability
resolves every step at rank 1. The only drift anywhere in the repository is
`cross-tenant/tenant-b`: `rank 1: 9, rank 5: 2`. Both rank 5 resolutions are `role_ordinal` with
clause `rank_regressed+name_no_longer_matched`, which is the ranked locator absorbing two tenant
renames the overlay deliberately leaves undeclared.

**Escalation demo timing:** intervention raised within 10 seconds of the run starting, resumed
immediately on approval, 55.3s total wall clock including the time I spent inspecting the console.

**Ponytail debt ledger: 4 markers, 0 with no upgrade path.**

| file:line | simplified | ceiling | upgrade trigger |
|---|---|---|---|
| `safety/risk.py:132` | `Key` classified `SAFE_REVERSIBLE` unconditionally | a `Key("Enter")` can submit a form and commit state like a Click | if any code path ever emits a `Key` action |
| `escalation/store.py:158` | lockfile via `O_CREAT\|O_EXCL`, no lease | a lock left by a crashed holder blocks that one intervention id | a lease or expiry on the lock file |
| `replay/recovery.py:219` | retry reloads the whole page | discards unsubmitted form state on that page | frame-scoped reload, needs `Surface` to expose it |
| `surface/locator.py:35` | `RelationalHint.kind` is a one-value `Literal` | only `row_label` was measured on this target | when a real target needs a second kind |

All four appear in REPORT.md section 7. Nothing in the ledger was dropped as not worth mentioning.

**Ponytail audit: 5 findings, all 5 applied.** The audit was a whole-repo sweep, not a diff review:
an AST pass for definitions never referenced anywhere, a dependency-by-dependency import check, a
policy-field reader check, and a scan for pure-delegate wrappers.

1. `delete:` `AmbiguousTarget` and `TargetNotFound` (`surface/locator.py`). Never raised, never
   caught, and their own docstrings admitted it. Replacement: nothing.
2. `delete:` `Redactor.redact_model` (`safety/redact.py`). A public two-line wrapper over
   `_redact_model_instance` with zero callers. Deleting it makes "one serialization path" literally
   true rather than nearly true.
3. `delete:` `Policy.max_action_retries` plus its entry in both policy YAMLs. No reader anywhere in
   `src/`. Retry limits actually come from `RecoveryRule.max_attempts`. This is exactly the dead
   field CLAUDE.md says to wire or cut.
4. `delete:` `scratchpad_fixture.log`, 60KB of stray working output at the repository root, not
   gitignored, so a reviewer would have seen it.
5. `fix:` the drift run discovery defect described in the gate above. Not an over-engineering
   finding but the audit is what surfaced it.

Checked and deliberately kept: every declared dependency is genuinely imported (`anyio` by
`catalog/server.py`, `pillow` by `redact.py`'s screenshot masking, and so on); the one-entry
`build_llm` registry and the `LLMClient` protocol stay because they are the provider seam ARCHITECTURE.md decision 56
defends; `desktop_stub.py` stays because it is the R7 deliverable; the named pure predicates in
`outcomes.py` and `recovery.py` stay because they are the registry vocabulary ADR 0014 specifies.

### Claim-to-file mapping  (REPORT sections 1 to 6)

Every claim maps to a file. Condensed, with the citations I checked personally marked:

| Claim | File | Checked |
|---|---|---|
| Two paths, one `Capability` contract | `models/artifact.py` | yes |
| No model in replay | `replay/engine.py`, `tests/test_constraints.py` | yes, 5/5 pass |
| `Surface` seam, `observe`/`act` | `surface/base.py` | yes |
| Desktop UIA seam, does not run | `surface/desktop_stub.py` | yes |
| `PolicyGate.dispatch` sole call site | `safety/policy.py`, invariant 2 | yes |
| Single process, synchronous escalation | `escalation/control.py`, `escalation/operator_app.py` | yes |
| Python 3.11, Pydantic v2, typer, FastAPI, ruff/mypy/pytest | `pyproject.toml`, `docs/adr/0001` | yes |
| Playwright as a library, headed; `aria_snapshot` per step | `surface/web.py`, `docs/adr/0002` | yes |
| Hostile fixture | `fixtures/legacy_bank/app.py` | yes, 5 synthetic members |
| Gemini, two models actually used | `llm/gemini.py`, both artifacts' `provenance` | yes |
| Eight tools all require `rationale` | `agent/tools.py` | yes, corrected citation |
| System prompt location | `agent/prompts.py` | yes, corrected citation |
| Diff only when digest unchanged | `agent/loop.py`, `docs/adr/0010` | yes |
| Schema fields, two version counters | `models/artifact.py`, `docs/adr/0012` | yes |
| Ranked `TargetDescriptor` | `surface/locator.py`, `docs/adr/0006` | yes |
| Recorder as separate pass | `record/recorder.py` | yes |
| Transcript decoupling | invariant 4 | yes |
| Append-only JSON storage | `docs/adr/0011`, `cli.py` | yes |
| Outcome, recovery, checkpoint order | `replay/engine.py`, `docs/adr/0014` | yes |
| 4 result kinds, 13 failure categories | `models/result.py` | yes, counted live |
| 6 ranked strategies, unique match required | `surface/locator.py` | yes, counted live |
| `WebSurface.pause` the one deliberate delay | `surface/web.py` | yes |
| Five measured defects | ADRs 0009, 0006, 0013, 0014, 0018 | yes |
| `TenantOverlay`, `resolve_for_tenant`, `OverlayError` | `models/artifact.py`, `docs/adr/0018` | yes |
| `app_fingerprint` signal, never a gate | `models/observation.py`, `replay/engine.py` | yes |
| Tenant B rank 1 to rank 5 degradation | `evidence/cross-tenant/tenant-b/run.jsonl` | yes, reproduced |
| Two heterogeneity limits | `replay/outcomes.py`, `docs/adr/0018` | yes |
| Subaccount artifact exists only via escalation | `evidence/discovery-subaccount/` seq 22 to 25 | yes, read verbatim |
| 7 stopping conditions, 8 reason codes, 4 control states | `agent/loop.py`, `models/intervention.py`, `escalation/control.py` | yes, counted live |
| Allowlist over every loaded frame URL | `safety/policy.py`, `surface/base.py` `urls()` | yes |
| Risk blocked not confirmed, label then route | `safety/risk.py`, `docs/adr/0007` | yes |
| Redaction one path, screenshot masked pre-write | `safety/redact.py` | yes |
| The inert route check admission | `docs/adr/0007` update section | yes, matches verbatim |

### Defence-list walk  (CLAUDE.md's deliverables contract)

CLAUDE.md's list has seven items, not the eight the phase prompt states. I walked all seven and each
is covered:

1. Language, runtime and frameworks: **Architecture**. Absent in round 1; added in round 2.
2. LLM provider and model, and how the loop is prompted and structured: **Architecture**.
3. The computer-use technology: **Architecture**. Absent in round 1; added in round 2.
4. The target application: **Architecture**.
5. Artifact schema, and how it is stored and serialized: **Artifact schema**.
6. Determinism on replay, locator strategy, fallbacks, waiting: **Determinism & error handling**.
7. Architecture and boundaries, single process versus services and synchronous versus queued:
   **Architecture**.

### Em dash audit  (per file, and what I changed)

| file | em dashes | action |
|---|---|---|
| README.md | 0 | new this phase, written without them |
| REPORT.md | 0 | new this phase, written without them |
| ARCHITECTURE.md | 0 | already clean, uses `--`, left alone |
| evidence/README.md | 0 | already clean, left alone |
| docs/adr/*.md | 28 (in 0004, 0005, 0008) | deliberately left |
| docs/reports/*.md | 372 across 14 files | deliberately left |
| CLAUDE.md | 18 | deliberately left, it is your instruction file, not a deliverable |

The ADRs and phase reports are point-in-time records carrying measured claims. Rewriting them now
risks changing a claim and reads as retrospective polish, so they stay as written.

### How the core piece works  (plain English)

The whole system turns on one asymmetry: the model is expensive, slow, and non-deterministic, so it
runs exactly once. Discovery gives an LLM a goal and a live browser, shows it the page as a flat
indexed list derived from the accessibility tree rather than HTML, and lets it propose actions
through validated tool calls that must each carry a stated reason. Every proposed action goes
through one function, `PolicyGate.dispatch`, which is the only place in the codebase that touches
the browser, so safety is a property of the code rather than a promise about the prompt. When the
goal is verified by a deterministic checkpoint the model does not get to self-declare, a separate
recorder pass reads the run log and writes a typed capability artifact: ordered steps, ranked
descriptors for finding each element, typed inputs and outputs, known business outcomes, and
recovery rules. Replay then executes that artifact with no model anywhere, resolving each element by
walking six ranked strategies until exactly one matches, and after every step it checks known
business outcomes first, recovery rules second, and the step's postcondition last, so "no such
member" comes back as an answer rather than a crash. When the gate refuses something irreversible,
or the session dies, the run stops and hands a human the same live browser window it was already
using, waits, and resumes from where it left off rather than starting over.

### Decisions logged

No new ADRs this phase, deliberately. Phase 14's job is to turn the existing eighteen ADRs into
REPORT.md, and the two decisions this phase actually made are recorded where a reviewer will see
them rather than in a nineteenth ADR nobody opens: the four deletions are named in REPORT.md
section 7, and the drift defect is section 7's newest example of the test-weakness pattern. The one
decision recorded only here is that no `understudy` console script was added: the module invocation
already works, CLAUDE.md's conventions prescribe running through the venv interpreter explicitly,
and a packaging change in the final phase buys nothing but a reinstall step.

### Caveats / not done

- **FIXED in round 3: a malformed `--params` now exits 2 with one line.** It previously escaped as
  a Rich traceback with exit 1, contradicting the exit-2 caller-error contract the CLI prints in its
  own help. `cli.py` now catches `json.JSONDecodeError` and prints
  `--params must be a JSON object: <decoder reason>`. Verified on the exact case a reviewer hits,
  the bash quoting form pasted into PowerShell: one line, `EXITCODE:2`, no traceback. I severed the
  guard and confirmed `tests/test_phase14.py::test_malformed_params_exits_2_with_no_traceback` goes
  red with `assert 1 == 2`, then restored it.
- **RETRACTED: my `_loaded_urls` caveat was wrong, and I checked it rather than acting on it.** I
  had written that it "returns an empty list if absent", leaving the allowlist nothing to check. It
  does not. The code is `if urls_method is None: return [surface.url]`, so the allowlist and the
  route-risk layer always get a real URL. Nor is it the shape ARCHITECTURE.md decision 76 fixed:
  that one reported success for work never done, whereas this returns a COMPLETE answer for the
  object it is handed, since a test double has no child frames and the Phase 7 defect was specific
  to a real frameset. The branch is also unreachable in production (`WebSurface` implements
  `urls()`; `DesktopSurface.urls()` raises rather than degrading), and removing it would mean adding
  `urls()` to 24 test doubles across 9 test files, which `surface/base.py`'s own docstring documents
  the pattern to avoid. No change made. Recording the retraction because the original caveat is the
  kind of confident wrong reading this project's conventions exist to catch, and it nearly caused a
  large edit to the test suite in the final phase.
- **I ran a real discovery run and then removed the `v4` artifact it produced.** The run itself is
  reported above with its real output. I removed the file because the catalog publishes only the
  highest version per capability and refuses any `draft`, so leaving a draft v4 would have made the
  catalog refuse the balance capability that the approved v3 currently serves, degrading the demo
  for a reviewer. Its evidence was written outside the repository.
- **The builder's round 1 report claimed no PowerShell tool was available** and used
  `powershell.exe` as a subprocess instead. I re-ran both `--params` forms myself through the real
  PowerShell tool; the results above are mine, not its.
- **`understudy` is not a command.** Every documented invocation is `python -m understudy.cli`. If
  you would rather the README read as `understudy discover ...`, that is two lines in
  `pyproject.toml` plus a reinstall, and I deliberately did not add it.
- **The fixture app is left running on 127.0.0.1:5055** so the demo path works if you try it now.
  Both operator consoles I started are stopped.
