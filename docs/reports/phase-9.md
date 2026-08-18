## Phase 9 Verification Report
Status: COMPLETE

Loop summary: five rounds. Round 1 (foundation) landed the single terminal-event writer, the
`APP_ERROR` category, `TargetDescriptor.recorded_rank`, four new surface signals, and the fixture
changes that make an injection mode reachable from a replayed capability; it passed first time.
Round 2 (the core) built `replay/outcomes.py`, `replay/recovery.py`, the engine rewrite, the
recorder changes and the CLI, and every machine gate passed, but the builder reported honestly that
rows 7, 9 and 10 of the test table did not reach their definition of done and had written tests
asserting the wrong behaviour. Those three went back with the real cause of each, plus the drift
baseline, and all four were fixed. Round 3 was one focused change: two HardFailure branches
reported a step number and then a restatement of the code's own control flow, which does not
satisfy R3's "report what step, what was expected, what was observed". Round 4 applied the
`/ponytail-review` delete-list. Round 5 reverted one item of that delete-list that made the code
worse, and turned the fifth review finding into the outcome-message decision. Both are written up
below rather than folded away, because in both cases the review itself was the thing that was
wrong.

Delegation: the builder wrote all of `replay/outcomes.py`, `replay/recovery.py`, the
`replay/engine.py` rewrite, the recorder changes, the surface signals, the fixture changes, the CLI
changes, and `tests/test_phase9.py`. The main session set the evaluation order and the taxonomy,
found the third parameter leak the phase prompt did not name (below), rejected the guessed drift
baseline, produced the v3 artifact, drove every live replay in this report itself, and wrote
ADR 0014.

### One thing the phase prompt did not name, found before delegating

The prompt identified two places member 12345 leaks into the capability: a derived accessible name
and a `frame_path` segment. There is a third. The recorded success checkpoint is
`text_present "$1,204.55"`, which is member 12345's balance, chosen by the model at `finish` time.
Every locator can resolve perfectly for member 22222 and the run still fails its checkpoint, so the
typed input stays a decoration and definition-of-done item 2 cannot pass. The recorder now
generalizes a `text_present` checkpoint whose value equals an `extract` step's own recorded output
into `element_present` against that step's target: the same assertion with the invocation-specific
data removed. Without this, item 2 fails.

### Machine-checkable gate  (ALL green for COMPLETE)

The test table. Every row below was driven by me from the CLI against the live fixture, in addition
to being asserted in `tests/test_phase9.py`. Rows 5 to 11 use a scratch copy of the artifact whose
`target.entry_point` is `http://127.0.0.1:5055/login?inject=<mode>`, which is the only door into a
fresh browser session's injection mode; the copies live outside the repository and nothing under
`artifacts/` was edited.

- [x] 1 happy path — ran: `replay --artifact ...v3.json --params '{"member_id": 12345, "password": "hunter2"}'` — got: `{"kind":"success","outputs":{"savings_balance":"$1,204.55"},"steps_run":7}`, exit 0
- [x] 2 THE PARAMETER IS REAL — ran: the SAME artifact with `member_id: 22222` — got: `{"kind":"success","outputs":{"savings_balance":"$532.10"},"steps_run":7}`, exit 0. Side by side: 12345 returns `$1,204.55`, 22222 returns `$532.10`. Step 5's recorded target is `name=":member_id.*"` with `name_match="regex"` and step 6's `frame_path` is `["contentframe", "/member/:member_id/balance"]`; neither embeds 12345.
- [x] 3 not_found — ran: `--params '{"member_id": 99999, ...}'` — got: `kind: business_outcome`, `code: member_not_found`, `message: "No member found for the given id."` (the capability's declared meaning), `observed: "No member matches that search."` (the app's own words), exit 0
- [x] 4 permission_denied — ran: `--params '{"member_id": 55555, ...}'` — got: `code: permission_denied`, `message: "You do not have permission to view this record."`, `observed: "You do not have permission to view member 55555."`, exit 0
- [x] 5 validation — ran: `?inject=validation` — got: `code: validation_rejected`, `message: "The submitted value could not be validated."`, `observed: "Member ID could not be validated. Please re-enter."`. The result carries both the declared contract and the application's own field text; see the outcome-message note below.
- [x] 6 unexpected_dialog — ran: `?inject=unexpected_dialog` — got: success, and one `recovered` event: `step=2 rule=dismiss_html_interstitial trigger=html_interstitial_present attempt=1 :: clicked the 'Dismiss' link on the HTML interstitial`
- [x] 7 native_dialog — ran: `?inject=native_dialog` — got: success, and three `recovered` events for `dismiss_native_dialog` at steps 2, 4 and 5 (3, 1 and 2 dialogs), each carrying the dialog text: `the browser's own handler dismissed 3 native dialog(s): confirm: Are you sure?; ...`. A separate rule and a separate mechanism from row 6: no DOM control is clicked, the `page.on("dialog")` handler answers it.
- [x] 8 slow_load — ran: `?inject=slow_load` — got: success, and two `recovered` events: `rule=wait_for_slow_load trigger=navigation_still_in_flight :: waited up to 8000ms for the in-flight navigation to settle` at steps 4 and 5. No fixed sleep (see item 15).
- [x] 9 transient_failure — ran: `?inject=transient_failure` — got: success, and exactly two `recovered` retry events, both at step 2, `backoff_ms=250` then `backoff_ms=500`. The interval strictly grew.
- [x] 10 session_expired — ran: `?inject=session_expired` — got: one `recovered` event `rule=reauth_on_session_expiry attempt=1 :: re-authenticated via the recorded login steps`, and then `hard_failure`, `category: session_expired`. Recovery is attempted and does not stick; see caveats.
- [x] 11 app_error — ran: `?inject=app_error` — got: `hard_failure`, `category: app_error`, `evidence_refs: ["steps/002_before.png","steps/002_after.png","dom/002.html","a11y/002.json","trace.zip"]`, exit 1
- [x] 12 bad param type — ran: `--params '{"member_id": "not-a-number", ...}'` — got: `category: invalid_params`, `observed: "parameter 'member_id' declared type 'integer' but got 'not-a-number'"`, `evidence_refs: []`, exit 2. No browser launched: the check runs before `WebSurface` is constructed, and `evidence_refs` is empty because there was no surface to capture from.
- [x] 13 all of the above with no API key — ran: every command in this report is prefixed `GEMINI_API_KEY= `, and `GEMINI_API_KEY= .venv/Scripts/python.exe -m pytest` — got: `212 passed`
- [x] 14 invariant 1 passes and is meaningful — ran: the invariant's own AST walk over `src/understudy/replay/` — got: 4 files, 1571 lines, 108 modules reached transitively, none of them `understudy.llm`, `understudy.agent`, `understudy.config`, or a provider SDK
- [x] 15 no fixed sleeps — ran: `grep -rn "time.sleep\|wait_for_timeout" src/understudy/replay/` — got: no output. The one deliberate timed delay in the system is `WebSurface.pause`, used only by the retry rule's backoff, documented as such in `surface/web.py`.
- [x] 16 degraded locator emits drift and still succeeds — see row 19, which is the concrete case
- [x] 17 unknown detector fails loudly at load — ran: `replay --artifact ...v2.json` — got: `invalid artifact: unknown outcome detector 'balance_check'; known detectors: ['member_lookup_no_match', 'permission_denied', 'validation_rejected']`, exit 2. Raised before any browser launches.
- [x] 18 no `insufficient_funds` — see the section below
- [x] 19 the Phase 6 case — ran: a copy of v3 with step 0's target name changed to `NoSuchControlAnywhere` — got: `{"kind":"success","outputs":{"savings_balance":"$1,204.55"}}` AND `locator_drift {"step_id": 0, "recorded_name": "NoSuchControlAnywhere", "recorded_rank": null, "actual_rank": 4, "strategy": "relational", "clause": "name_no_longer_matched"}`. The step still resolves and the run still succeeds, which is correct, and the warning now names the recorded name that stopped matching. Before this phase that mutation passed in total silence.
- [x] 20 five runs, identical results, stability written — ran: `replay ... --repeat 5` — got: `stability: {"runs":5,"successes":5,"last_n_outcomes":["success","success","success","success","success"],"computed_at":"2026-08-18T02:04:56.688645+00:00"}`, and that field is now populated in the artifact. All five returned `savings_balance: "$1,204.55"`. Nothing gates on it.
- [x] 21 every act event carries the step's rationale — ran: reading `evidence/replay-f1f9bc710bb9/run.jsonl` — got three lines:
      `step 0  type   rationale: Type 'admin' into Username field.`
      `step 1  type   rationale: Type '[REDACTED]' into Password field.`
      `step 2  click  rationale: Click Login button to log into the application.`
      No model was in the loop; these are the model's own recorded words, promoted verbatim by the recorder.
- [x] 23 ADR exists — `docs/adr/0014-outcome-before-checkpoint-and-named-predicates.md`
- [x] 24 this report is saved — `docs/reports/phase-9.md`

Supporting gates:

- [x] test suite — ran: `.venv/Scripts/python.exe -m pytest` — got: `212 passed in 81.10s`, no skips with the fixture running (42 of them in `tests/test_phase9.py`)
- [x] lint — ran: `.venv/Scripts/python.exe -m ruff check .` — got: `All checks passed!`
- [x] types — ran: `.venv/Scripts/python.exe -m mypy src/` — got: `Success: no issues found in 34 source files`
- [x] exit codes — ran: five replays, one per class — got: `success exit=0`, `business_outcome exit=0`, `hard_failure exit=1`, `bad param type exit=2`, `unknown detector exit=2`

### Item 18: what each kept outcome earned

The re-recorded capability declares three known outcomes, not the two seeded unconditionally
before. `insufficient_funds` is gone, and so is its `balance_check` detector, which was a name with
no implementation.

- `member_not_found` (detector `member_lookup_no_match`). Earned by steps 3 and 4: the flow types
  into a control named "Member ID" and clicks one named "Search". A flow that looks a record up can
  legitimately answer "there is no such record".
- `permission_denied` (detector `permission_denied`). Earned by step 5, whose postcondition is a
  URL under `/member/`: the flow opens a protected record, and the application can refuse it.
- `validation_rejected` (detector `validation_rejected`). Earned by step 3 followed by step 4: the
  flow submits typed input, and a form that takes typed input can reject it.
- `insufficient_funds` is not emitted. No step types into a deposit, amount or transfer field and
  no step is classified RISKY_IRREVERSIBLE, so this read-only lookup spends nothing and cannot
  produce the outcome. It was a false contract in the artifact and a dead detector in replay's hot
  path.

The gate for a recovery rule is a different question, and deliberately so: a slow load, a dialog or
a transient 503 can happen to any flow regardless of what one recording hit, so gating those on
"did this run hit one" would strip every rule from every clean recording. Recovery rules are gated
on whether replay can actually perform them, which is why `reauth_on_session_expiry` is emitted
only when the flow has recorded login steps to re-run. A unit test covers a flow with no login
prefix not getting the rule.

### The ponytail-review pass, and one finding I got wrong

CLAUDE.md requires `/ponytail-review` on the diff. It produced five findings. Two were real cuts:
an unused `logger` parameter on `recovery.apply()` that existed with a justification attached, and
two `getattr` degrade branches in `_apply_retry`/`_apply_wait` that returned a string saying
nothing had been retried or waited on, which the engine then logged as a `recovered` event. That
second one is not a tidiness item. It is the same failure shape as the Phase 5 mutating-route check
that read only the frameset shell URL and the Phase 7 inert route check: a mechanism that is
present, does nothing, and reports that it did something, so the evidence log lies. Those branches
are gone and recovery's surface parameter is now typed `WebSurface`, so an absent method raises at
the call site and mypy checks the requirement in advance.

Two findings were mislabelled as shrinks and my line estimate was badly wrong: the review predicted
-52 lines and the four applied items came to +2. `_param_type_error` grew from 32 lines to 45 and
was kept, because it now writes its message once instead of four times and names its two
non-trivial predicates. `_seed_known_outcomes` was converted to a table of
`(KnownOutcome, predicate)` pairs and then **reverted**, because making the table uniform required
three signature-adapting lambdas whose only job was to fake a shared shape, and the consistency
argument for it was wrong anyway (`_RECOVERY_RULE_SEEDS` carries no predicates, so the two were
never parallel). Optimising for line count produced indirection in the file a reviewer reads
hardest. The three plain if-blocks are back, and the seeded outcomes were verified byte-identical
to v3 by rebuilding the capability in memory and comparing field by field.

The fifth finding turned into a design decision rather than a cut, below.

### The outcome message contract, changed during review

`KnownOutcome.message_template` was write-only. The recorder wrote it on all three seeded outcomes
and the only reader was `message = matched_text or outcome.message_template`, whose `or` cannot
execute because the text scan never returns an empty string. Measured live before the fix: the
artifact seeds "No member found for the given id." and replaying member 99999 returned the app's
"No member matches that search."

Deleting the dead branch would have made a serialized schema field unreachable rather than removing
dead code, so the direction was reversed. `message` is now the declared template, `BusinessOutcome`
gained an `observed` field carrying the application's literal text, and the two fields are both
asserted in rows 3, 4 and 5. The deciding argument is Phase 12: two tenants of the same vendor
product will render different strings for the same outcome, and a caller whose `message` changes
per tenant has a contract the artifact was supposed to absorb that drift for. Full reasoning in
ADR 0014, decision 5.

### Human-review items  (the user confirms these)

- [ ] Item 22, a HardFailure a person can act on. Check: the block below, or re-run
      `GEMINI_API_KEY= .venv/Scripts/python.exe -m understudy.cli replay --artifact <copy with entry_point .../login?inject=app_error> --params '{"member_id": 12345, "password": "hunter2"}'`.
      What you should see: the step named, what that step was trying to achieve, and what was
      actually on the screen instead.

```json
{
  "kind": "hard_failure",
  "step_id": 2,
  "category": "app_error",
  "expected": "step 2 (click 'Login'): the URL 'http://127.0.0.1:5055/members' to be loaded",
  "observed": "app_error detected: An unexpected error occurred.",
  "evidence_refs": ["steps/002_before.png", "steps/002_after.png", "dom/002.html",
                    "a11y/002.json", "trace.zip"]
}
```

  A postcondition failure, for contrast, reports what was actually seen rather than restating the
  check:

```json
{
  "kind": "hard_failure",
  "step_id": 6,
  "category": "postcondition_failed",
  "expected": "step 6 (extract from generic 'Savings Balance'): a generic named 'Not The Real Label' to be present",
  "observed": "current URL(s): http://127.0.0.1:5055/app, http://127.0.0.1:5055/nav, http://127.0.0.1:5055/member/12345, http://127.0.0.1:5055/member/12345/balance; no generic named 'Not The Real Label' was present"
}
```

- [ ] The v3 artifact reads as a reusable capability rather than one member's transcript. Check:
      `artifacts/look-up-member-12345-and-read-their-current-savings-balance.v3.json`. What you
      should see: `member_id` typed `integer` with example `12345` (an int, not a string), step 5
      targeting `":member_id.*"` by regex, step 6's frame path carrying `:member_id`, a success
      checkpoint that is not one member's balance, three earned outcomes and five recovery rules
      naming registered conditions.
- [ ] The four fixture changes are fair. Check: `fixtures/legacy_bank/app.py`. What you should see:
      arming a mode from `/login?inject=`, preserving it across login's `session.clear()`, the
      `unexpected_dialog` interstitial firing once instead of on every request, and the
      `transient_failure` counter keyed on the session instead of on `request.path`. None reduces
      hostility; each makes an existing mode reachable from a replayed capability. Reasoning is in
      the caveats below.

### Invariants

```
$ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -q
.....                                                                    [100%]
5 passed
```

No skips. Invariant 1 is now load-bearing rather than nominal: it walks 1571 lines across four
files under `replay/` and follows 108 transitively reached modules, none of which is an LLM client
or a provider SDK.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

- Replay stability: 5 runs of the happy path, 5 successes, identical `savings_balance` of
  `$1,204.55` every time. Duration 2.09s to 2.30s per run headed.
- Locator drift on the happy path: zero events across all five runs. Under the drift rule that
  means all seven steps resolved on a name-matching rung. The exact rank per step is not logged on
  success today, only on drift or on failure, so the full rank distribution is not something I can
  report honestly this phase. Noted as a gap below.
- Recovery attempts actually exercised: 1 (HTML interstitial), 6 native dialogs across 3 events,
  2 retries at 250ms then 500ms, 2 waits, 1 reauth.
- Row 8 wall time with the 6-second server-side sleep injected: roughly 33 seconds for the run.
- Discovery runs this phase: none. See caveats.

### How the core piece works  (plain English)

Replay reads the artifact, resolves every detector and trigger name it declares against two
registries of real functions and refuses the artifact outright if any name is unknown, then checks
the caller's parameters against the declared types before a browser exists. For each recorded step
it interpolates the caller's parameters into the step's value, the target's accessible name and the
target's frame path through one shared substitution function, resolves the target through the
ranked descriptor list, dispatches through the policy gate, and takes a fresh observation. It then
asks three questions in a fixed order: is the screen showing a legitimate business answer such as
"no member matches that search", in which case the run ends successfully with that answer and exit
code 0; failing that, does a declared recovery rule's condition hold, in which case it dismisses,
retries with real exponential backoff, waits, or re-authenticates, and re-evaluates the same step;
failing that, is the screen showing an unrecovered condition it can name precisely, such as an app
error page or a lost session. Only when all three come back empty does it check the step's recorded
postcondition. No model is involved in any of it, and the log still explains every action because
each one carries the rationale the model gave when the capability was first discovered.

### Decisions logged

- `docs/adr/0014-outcome-before-checkpoint-and-named-predicates.md` — detectors and triggers are
  named predicates resolved at load, the evaluation order and why it is that order, seed gating on
  two different axes, and drift reported without inventing a baseline.

### Caveats / not done

- **No live discovery run this phase, by choice.** The v3 artifact was produced by re-running the
  recorder pass over the genuine Phase 8 discovery evidence in `evidence/discovery-b2405e162ba4`,
  via a new `record --run-dir` CLI command. The recorder is a separate pass over a written
  `run.jsonl` by design, so this is real produced output from a real model run, not authored data,
  and it isolates the recorder change instead of confounding it with a different flow the model
  might have taken. R8's non-negotiable live discovery run is the existing evidence, unchanged. It
  does cost one thing: `recorded_rank` is `None` on every step of v3, because the recorder reads
  descriptors out of a log written before that field existed. Drift detection still works through
  the second clause of `_drift_reason`, which is exactly why that clause exists.
- **`artifacts/*.v2.json` no longer loads, deliberately.** It declares the detector `balance_check`,
  which nothing implements and which the recorder no longer emits. Failing loudly on it is the
  behaviour item 17 asks for. v3 supersedes it; artifacts are append-only, so v2 stays on disk as
  the record of what the previous recorder produced.
- **Four fixture changes.** Without them, four rows of the table are unreachable from a replayed
  capability, because replay drives a fresh browser session that never makes the same-session
  `/admin/inject` request a live operator would. `/login?inject=` arms a mode and login preserves
  it across its own `session.clear()`. `unexpected_dialog` fires once rather than on every request:
  its Dismiss link drops the query string, so a persistent mode silently destroys a search's own
  `?f7=` parameter, which makes the mode untestable rather than hostile. `transient_failure` counts
  per session rather than per URL path, because this app is a frameset and one logical page load is
  three separate paths each with an independent counter, which is an accident of the fixture's
  shape and not what a brief outage looks like. `validation` now also applies to the member search,
  not only the subaccount POST. Nothing was sanitized and no locator got easier.
- **Row 10 ends in a hard failure, and that is the honest result.** The reauth recovery fires and
  re-runs the recorded login steps on the same session, but the test copy's entry point carries
  `?inject=session_expired`, so re-navigating to it re-arms the injection and the session dies
  again. The rule's budget of one attempt is what stops the run rather than looping. The definition
  of done names `HardFailure session_expired` as the expected outcome when reauth does not carry
  the run, and both halves are asserted: the recovery event exists and the category is
  `session_expired`. The other half of that row, reauth being unavailable at all, has no live path,
  so it is covered by a unit test asserting `recovery.unrecovered` returns `SESSION_EXPIRED` for a
  capability with a login prefix and no reauth rule.
- **`session_lost_mid_flow` cannot distinguish a wrong password from an expired session.** Both
  land back on the login form after the login click. The rule reads it as a session loss, retries
  once, and reports `session_expired`. That is an honest limit of judging state from the rendered
  screen alone, documented in the trigger.
- **The retry rule reloads the whole page.** That discards unsubmitted form state. It is marked
  with a `ponytail:` comment naming the ceiling and the upgrade path, a frame-scoped reload, which
  a flow that types across a transient failure would need. This flow does not.
- **Locator rank is not logged on a successful resolution**, only on drift or failure, so the
  distribution asked for under run-and-report is only available as "zero drift, therefore all
  name-matching rungs". One field on an event that already exists would close it. Left for
  Phase 12, where per-tenant rank drift is the actual consumer.
- **`UNHANDLED_DIALOG` raised from a blocked `observe()`** still carries the generic
  `expected` wording that rounds 3 replaced in the other two branches. Its `observed` already
  carries the dialog type and message, so it is legible; it is simply less consistent than the rest.
- **Nothing verifies that the live tests run against a fixture serving current code, and this
  phase got caught by it.** Row 9 failed with `assert 15 == 2` after the transient-failure counter
  was changed from per-path to per-session. The fix was correct; the Flask process answering on
  port 5055 had been started hours earlier and was still serving the pre-fix module. It was
  diagnosed by decoding the session cookie, which showed `{"attempts": {"/app": 7}}`, the old
  per-path structure that the new code cannot produce. Nothing in the test setup or the skip guard
  checks anything except that something answers on the port.

  This is the fifth time in this build a check has been green, or red, over behaviour it was not
  actually measuring: the Phase 5 policy test that passed while the mutating-route check read only
  the frameset shell URL, the Phase 7 route check that was present and inert, the Phase 8 pruning
  test whose synthetic data could not express the sequence that broke it, and the Phase 4 ordinal
  pool where `describe()` and the resolver indexed different pools. This instance surfaced as a
  false red, which is the lucky direction. The same hole permits a false green just as easily: a
  live row asserting recovery behaviour would pass happily against a stale server still serving the
  code that behaviour was written to replace. The fix is small and not done: have the fixture
  expose a build identity (a hash of `app.py`, or a `/admin/version` route) and have the live-test
  skip guard compare it against the working tree, skipping loudly on a mismatch instead of testing
  a ghost.

- **`evidence/` went from 32 entries to 47, and 15 of those are mine.** Ten `replay-` directories
  dated 8/17 21:23-21:24 are the definition-of-done drive-through and the exit-code sweep, and five
  dated 22:04 are the `--repeat 5` stability run. All are genuine produced output and none is
  authored, but they are inside the repository because those CLI commands did not pass
  `--evidence-dir`, which defaults to `evidence/`. `tests/conftest.py` guards the test suite
  against exactly this and its own docstring records the Phase 6 occurrence, but it cannot see a
  manual CLI run, which is the same hole Phase 5 left open. Phase 13 curates `evidence/` down to
  the runs a reviewer should actually read; until then the count is 47 and the extra 15 are
  accounted for above.
