## Phase 6 Verification Report
Status: COMPLETE

Loop summary: 2 rounds. Round 1 delivered the whole scope, and surfaced one real conflict with the
phase brief rather than quietly working around it (see "The one deviation" below). Independent
verification then found one defect the builder's own checks passed over: `evidence_refs` were
serialized with the host OS's native path separator, so a `HardFailure` produced
`steps\000_before.png` on Windows and `steps/000_before.png` on Linux, which is not a stable contract
for the agent that consumes the result. Round 2 fixed it, and found two more call sites than my
report named (five, not three), which I confirmed.

Delegation: builder wrote the `RunEvent` schema, the `EvidenceLogger` rewrite, the four-kind result
union, the failure classification, perception versioning, the caller rewiring across loop, engine,
CLI, recorder and gate, `tests/conftest.py`, `tests/test_phase6.py`, and ADR 0009. Main session
corrected the carry-forward's stated cause before delegating, fixed the error taxonomy and the
classification precedence rules, ruled on the result-model renames and which test migration was
permitted, reproduced the path-separator defect live, and verified the whole thing independently.

### A correction to the carry-forward, made before delegating

The brief said the navigation-guard tests wrote directories into `evidence/`. That is not the
mechanism. `grep -rn "EvidenceLogger(" tests/ src/ | grep -v base_dir` returns zero test call sites,
every existing test already passes `base_dir=tmp_path`, and no test calls `replay()` at all. The two
directories came from manual CLI verification runs during Phase 5. So the fix is not "correct the
tests", it is to close the structural gap that let a run write there at all:
`replay()` and both CLI commands now take an explicit evidence base directory, and
`tests/conftest.py` holds a session-scoped autouse fixture that fails the suite if `evidence/` gains
an entry. The two named directories were deleted after I confirmed each was a three-event
refused-navigate run with no screenshots and no artifact.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] **1. Full directory layout** — ran a real replay through the CLI against the live fixture app:
  ```
  replay-43ad78f66a4b/result.json
  replay-43ad78f66a4b/run.jsonl
  replay-43ad78f66a4b/steps/000_before.png ... 006_after.png   (7 pairs, 14 files)
  ```
  On success the trace is discarded and no `dom/` or `a11y/` is written, which is the specified
  behaviour. A forced failure produces the rest (item 5).

- [x] **2. Every line parses and validates against the event schema** — over that real run:
  ```
  lines: 10 | all validate against RunEvent
  act events: 8
  event types: ['act', 'replay_end', 'replay_start']
  seq monotonic: True
  ```
  Validation command: `RunEvent.model_validate(json.loads(line))` for every non-blank line. This is
  true by construction, not by inspection: every write goes through the one model.

- [x] **3. No act event has a null, empty or `[REDACTED]` rationale** — asserted in
  `test_t1_t2_t3_...` over a real generated discovery log, with `assert len(act_events) > 0` so it
  cannot pass vacuously. From my own live replay, the rationale on all 8 act events:
  ```
  step=None  "open the capability's recorded entry point"
  step=0     'Entering username.'
  step=1     '[REDACTED]'          <- inherited from the frozen Phase 2 artifact, see deviation
  step=2     'Clicking Login button.'
  step=3     'Entering Member ID 12345.'
  step=4     'Clicking Search button.'
  step=5     'Clicking the member link to view details.'
  step=6     'Extracting the savings balance.'
  ```

- [x] **4. No directory under `evidence/` is created by a test run** — `ls evidence/ | wc -l` returns
  28 before and after the full suite, and the two polluted directories are gone
  (`ls evidence/ | grep -c "651219783f3d\|e8387a53f726"` returns 0). Enforced permanently by the
  conftest fixture, which asserts the same thing at session end.

- [x] **5. A forced failure produces dom, a11y and trace, listed in `evidence_refs`** — ran the CLI
  against an artifact mutated programmatically in memory from the real one (never hand-written):
  ```json
  {"kind": "hard_failure", "step_id": 0, "category": "stale_perception",
   "expected": "a unique element matching role='nosuchrole' name=''",
   "observed": "role_name_exact: 0 candidate(s) ... role_ordinal: 0 candidate(s) (ordinal 0 out of
                range for 0 role='nosuchrole' candidate(s)); dom_fallback: 0 candidate(s) ...",
   "evidence_refs": ["steps/000_before.png", "steps/000_after.png", "dom/000.html",
                     "a11y/000.json", "trace.zip"]}
  ```
  All five files exist on disk. `trace.zip` is a real Playwright trace.

- [x] **6. A PII region is masked, verified programmatically** —
  `test_t5_logger_screenshot_masks_only_the_flagged_region_through_the_real_path` goes through the
  real logger path (not `redact_screenshot` directly), reads the PNG the logger actually wrote, and
  asserts `masked.getpixel((4,4)) != original.getpixel((4,4))` inside the bounds and
  `masked.getpixel((15,15)) == original.getpixel((15,15))` outside.

- [x] **7. The result union round-trips for all four kinds** —
  `test_t6_all_four_result_kinds_round_trip_through_the_union` dumps and revalidates each kind and
  asserts the `kind` discriminator selects the right class.

- [x] **8. Invariant 3 against a real generated log from this phase** —
  `test_t7_sentinel_absent_from_a_real_generated_run_log` checks utf-8, url-encoded and base64
  forms. `pytest tests/test_constraints.py -rs` → `5 passed`, no skips.

- [x] **9. This report saved** — `docs/reports/phase-6.md`.

Supporting gates:
```
$ pytest -q                 -> 128 passed, 0 skipped (fixture app up, so both live tests ran)
$ ruff check .              -> All checks passed!
$ mypy src/                 -> Success: no issues found in 31 source files
$ grep -rn "\.act(" src/    -> one call site, safety/policy.py:276, inside PolicyGate.dispatch
```

### The one deviation from the brief, and why it is not a weakened check

The brief said the `[REDACTED]` rationale check should live in the event schema. The builder hit a
real conflict and reported it rather than working around it, which I verified: the one artifact in
`artifacts/`, recorded from the genuine Phase 2 discovery run this project's non-negotiable depends
on, has `"rationale": "[REDACTED]"` on step 1. Phase 2's whole-string redaction rule destroyed the
model's real reason for typing the password, and that value is now frozen in real evidence that must
not be edited. Enforcing the literal rejection inside `RunEvent` made replay of that artifact raise
at step 1, turning the demo path's Success into a HardFailure.

The resolution: `RunEvent` enforces present and non-empty on every act event, in both paths, and the
literal-sentinel rejection moved to `agent/loop.py`, where it can be judged against a live model turn
rather than against a frozen historical artifact. The Definition-of-Done assertion itself was NOT
relaxed: `test_t1_t2_t3_...` still asserts `event.rationale != "[REDACTED]"` over a real generated
discovery log, with a non-vacuity guard. The relaxation is confined to what the schema rejects at
write time, and both sides are commented in place.

### Human-review items  (the user confirms these)

- [ ] **Whether to re-record the one artifact.** Check: step 1 of
  `artifacts/look-up-member-12345-and-read-their-current-savings-balance.v1.json` — what you should
  see: `"value": "[REDACTED]"` and `"rationale": "[REDACTED]"`, both destroyed by the Phase 2
  redaction bug that Phase 5 fixed. R5 grades the "why", and a reviewer reading that artifact sees one
  step whose reason is gone. One fresh discovery run with the current redactor would produce a clean
  artifact where the value becomes `${param:...}` and the rationale survives. It costs LLM quota
  (ADR 0003 measured the free tier at 20 requests/day) and evidence curation is nominally Phase 13,
  so this is your call, not mine. I have not re-run discovery.
- [ ] ADR 0009 defends the taxonomy — check:
  `docs/adr/0009-result-contract-and-failure-taxonomy.md` — what you should see: why four terminal
  kinds, why `Recovered` is an event rather than a kind, why the three locator-adjacent categories
  are separate, and a stated limit section on `stale_perception` being a version-mismatch heuristic.
- [ ] CI green after push. Depends on a push, so it can never be machine-checked here. The three
  commands CI runs are green locally. The two live tests will skip in CI, loudly, naming the missing
  fixture app and browser.

### Invariants

```
$ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -rs
.....                                                                    [100%]
5 passed in 0.23s
```
All five live, none skipped.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

No LLM discovery run this phase. Measured:

- Test suite: 128 tests, 0 skipped locally, 2 of them driving a real headed browser.
- Real replay of the recorded capability: 7 steps, 8 act events, 10 run.jsonl lines, 2172 ms,
  `savings_balance: "$1,204.55"`. Stable across the three runs I did during verification.
- Forced-failure replay: fails at step 0, 5 evidence refs, 33 KB trace.
- Locator resolution on the successful replay: every step resolved; the mutation experiment showed a
  descriptor with a recorded ordinal still resolves positionally when its recorded name matches
  nothing (see caveats).
- `evidence/`: 28 directories, unchanged by the suite.

### How the core piece works  (plain English)

Every run now writes one directory that a person can debug from without rerunning anything. Each line
of `run.jsonl` is one event built through a single schema, so the log cannot drift into ad hoc shapes,
and one field on it is mandatory for any action: the rationale. In discovery that is the model's own
stated reason, read straight off the tool call rather than invented afterwards; in replay there is no
model at all, so it is the reason recorded in the artifact when the step was first learned, which is
what lets a replay log still explain itself with nothing intelligent in the loop. Screenshots are
taken in pairs around each step and masked before the bytes hit disk. On failure, and only on failure,
the run also keeps a DOM snapshot, the accessibility tree it was looking at, and a Playwright trace,
and the failure result lists those files by path so the caller can find them. The result itself is one
of four things, and the distinction is the point: a success, a business outcome the caller asked about
and got a real answer to, a hard failure with a category naming what actually broke, or an escalation.
The categories exist because a dead server and an artifact recorded under older perception used to
produce the identical message, and one means go start the app while the other means go re-record the
capability.

### Decisions logged

- `docs/adr/0009-result-contract-and-failure-taxonomy.md` — four terminal kinds; `Recovered` as an
  event rather than a kind; three separate locator-adjacent failure categories; the stated limit that
  `stale_perception` is a version-mismatch heuristic.
- `ARCHITECTURE.md` — new "Phase 6 decisions" section, items 50 to 55.

### Caveats / not done

- **The artifact's one destroyed rationale.** Covered above as a human-review item. It is real
  historical damage from Phase 2, not a current bug, and the current code cannot produce it.
- **`RunEvent` allows extra keys.** `model_config = {"extra": "allow"}` so a `run_start` can carry
  goal/target/model and an act event can carry its context without padding every other event with
  nulls. The consequence is that "validates against the event schema" is a weaker statement than it
  sounds: unknown keys pass. The strong guarantee comes from construction instead, since every write
  goes through the model, and the typed named fields plus the act-event validator are what actually
  bite.
- **`stale_perception` will always win over `locator_unresolved` for the existing artifact**, because
  its `perception_version` defaults to 1 and the current version is 2. That is the specified
  precedence and ADR 0009 states the limit, but it does mean `locator_unresolved` is currently
  unreachable for that one artifact. A capability recorded from here on carries version 2 and will
  classify normally.
- **An ordinal still resolves positionally when the recorded name matches nothing.** Found while
  forcing a failure: mutating step 0's target name to `NoSuchControlAnywhere` did not fail the
  replay, because the descriptor also carries `ordinal: 0` and the ordinal rung indexes the role pool
  regardless of the name mismatch. Phase 4's decision 38 refuses exactly this rescue for the
  sole-candidate case but not for a recorded ordinal. It is arguably correct (an ordinal is recorded
  evidence, not an inference) but it is a drift signal being ignored, and it belongs in Phase 9's
  drift handling. Not changed here; out of this phase's scope.
- **`PERMISSION_DENIED`, `SESSION_EXPIRED` and `UNHANDLED_DIALOG` have no detector yet.** They exist
  as categories; Phase 9 wires them.
- **Screenshot cost went up.** Each step now takes two screenshots, and the "after" one needs a fresh
  `observe()` so the mask is positioned against the pixels it is actually masking. That is one extra
  accessibility snapshot per step, accepted deliberately: a mask placed from a pre-action observation
  lands in the wrong place, which is a leak rather than a cosmetic bug.
