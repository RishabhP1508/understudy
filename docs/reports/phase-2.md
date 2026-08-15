## Phase 2 Verification Report

Status: COMPLETE

Loop summary: 2 rounds, plus a pre-delegation probe phase that turned out to matter more than either.

Before writing the spec I probed the two APIs this phase depends on, against the live fixture, rather
than describing them from memory. That found three things that would each have cost a build round or
produced a wrong design:

- `page.accessibility` does not exist in Playwright 1.62.0. The old accessibility snapshot API has
  been removed, so the entire perception plan had to be built on `aria_snapshot` instead.
- One `page.aria_snapshot(mode="ai")` call on the frameset page returns the nav frame, the content
  frame, AND the depth-2 balance iframe in a single tree. The phase prompt assumed frame traversal
  would need writing and deferred it to Phase 3; measured, it needs no code at all.
- `gemini-2.5-flash` returns 404, "no longer available to new users".

Round 1 delivered the whole thread and it worked. My review found four real problems and one
completeness gap. Round 2 fixed all five. Details in the loop notes at the end.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] a real discovery run completes and writes genuine model tool calls — ran: the CLI myself, twice
  (see the authenticity section) — got: `status: goal_verified`, `evidence/discovery-3348784c8a88/`
  with `run_start`, 8 `dispatch`, `goal_verified`, `run_end` — expected: a real run
- [x] the artifact has at least three steps and a success checkpoint — ran: read
  `artifacts/look-up-member-12345-and-read-their-current-savings-balance.v1.json` — got: 7 steps and
  `success: {"kind": "text_present", "target": "page", "value": "$1,204.55"}` — expected: >= 3 steps
- [x] replay succeeds with GEMINI_API_KEY cleared — ran:
  `env GEMINI_API_KEY= .venv/Scripts/python.exe -m understudy.cli replay --artifact artifacts/look-up-member-12345-and-read-their-current-savings-balance.v1.json --params '{}'`
  — got: `{"kind":"success","outputs":{"savings_balance":"$1,204.55"},"steps_executed":7,"checkpoint_verified":true}`
  — expected: success with no model available
- [x] all five invariants pass NON-TRIVIALLY, zero skips — ran:
  `.venv/Scripts/python.exe -m pytest tests/test_constraints.py -v` — got: `5 passed in 0.17s`, no
  skip lines — expected: 5 passed, 0 skipped
- [x] a false checkpoint does not end the run — ran: `pytest tests/test_phase2.py` — got: passing
  `test_false_checkpoint_does_not_terminate_the_run`, which drives a fake LLM that always calls
  finish with text that is never on the page and asserts `status == "max_steps"`, not
  `"goal_verified"` — expected: the model cannot self-declare success
- [x] exactly one `.act(` call site, in PolicyGate.dispatch — ran: `grep -rn "\.act(" src/` — got:
  `src/understudy/safety/policy.py:26:        return surface.act(action)` and nothing else —
  expected: one hit
- [x] one definition of checkpoint semantics — ran:
  `grep -rn "def verify_checkpoint\|def checkpoint_satisfied" src/` — got: `checkpoint_satisfied`
  defined once in models/artifact.py, `verify_checkpoint` in loop.py delegating to it — expected: no
  duplicate
- [x] screenshots are actually written — ran: `ls evidence/discovery-3348784c8a88/` plus a PNG magic
  byte check — got: `step-0.png` through `step-7.png`, 8305 to 13539 bytes, all with a valid PNG
  header — expected: a screenshot per step
- [x] lint and types — ran: `ruff check .` and `mypy src/` — got: `All checks passed!` and
  `Success: no issues found in 29 source files` — expected: exit 0
- [x] whole suite — ran: `.venv/Scripts/python.exe -m pytest -q` — got: `10 passed` — expected: green
- [x] nothing sensitive is committed — ran: a sweep of evidence/ and artifacts/ for the real API key,
  Google and `AQ.` key shapes, SSN shape, and the test sentinel — got: `clean: no secret patterns in
  evidence/ or artifacts/` — expected: clean
- [x] the artifact carries no transcript — ran: a recursive key scan — got: `banned keys present:
  NONE`, `transcript_hash: 9e190f8669b375668db69a22...` — expected: a hash, not a transcript
- [x] docs/adr/0002-accessibility-tree-over-screenshots.md exists — yes, plus 0003 on the model choice
- [x] docs/reports/phase-2.md — this file

### Is the discovery run genuine? (R8, the one non-negotiable)

I did not take the builder's evidence on trust. Three things establish it:

1. **I ran discovery myself, twice.** Not a re-read of the builder's log, a fresh execution. Both
   produced their own run ids, their own timestamps, and their own transcript hashes.
2. **A different model reproduced it.** The builder's run used `gemini-flash-latest`. Mine used
   `gemini-3.1-flash-lite`, a different model family, and reached the same `goal_verified` in the same
   8 rounds with the same extracted `$1,204.55`. A fabricated log does not reproduce under a model
   swap.
3. **The log's internal detail matches the fixture's real structure.** The extract step targets
   `role=generic, name="", ordinal=2`, which is exactly the anonymous node holding `$1,204.55` inside
   the depth-2 iframe that I had independently found in my own Playwright probe before any code
   existed. Inter-event gaps are irregular in the way real model latency is (2.9s, 5.9s, 9.7s, then
   0.9s, 0.8s, 0.9s, 1.0s), not uniform. The rationales are ordinary model prose, and the model chose
   to type "admin"/"admin" at the login, which nothing in the code suggests.

Three discovery runs completed, all reaching goal_verified:

    model                    rounds  steps  rejected  prompt   completion  total tokens
    gemini-flash-latest        8       8       0      17,882      317       18,371   (builder)
    gemini-3.1-flash-lite      8       8       0      17,835      304       18,139   (mine)
    gemini-3.1-flash-lite      8       8       0      17,731      266       17,997   (mine, post-fix)

### Invariants

    $ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -v
    collected 5 items
    tests\test_constraints.py .....                                          [100%]
    ============================== 5 passed in 0.17s ==============================

Zero skips. All five are now live against real code rather than shaped stubs:

1. `replay/engine.py` imports only models/, surface/, safety/, evidence/. The AST walk follows the
   graph and finds no route to understudy.llm or a provider SDK.
2. `surface.act` is called in exactly one place in all of src/, inside `PolicyGate.dispatch`.
3. `Redactor.dumps` is the only write path, and it strips both sentinels in all three encodings using
   two general rules (SSN shape, credential-token substring), with no special case for the sentinel
   literals.
4. The Capability schema has no key named messages, transcript, completion, choices, or content, and
   `provenance.transcript_hash` is a real sha256 of the run log.
5. `FINISH_TOOL` requires `checkpoint`, and the finish branch in loop.py calls `verify_checkpoint`
   with no return before it.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

- Discovery: 8 rounds, 8 dispatched steps, **0 rejected or malformed turns** across all three runs.
- Tokens: ~18k total per run, of which ~17.8k is prompt. The cost is re-sending the accessibility
  rendering every round; nothing caches between rounds yet.
- Wall clock: 27 to 45 seconds per discovery run.
- Replay stability: 3 replays of the same artifact, 3 identical successes, same extracted value.
- Locator shape, the early read on what Phase 4 has to solve: of 7 recorded steps, **4 resolved on
  role plus accessible name alone and 3 needed an ordinal**. The three are the two nameless login
  textboxes and the anonymous `generic` node holding the balance. This is the Phase 1 prediction
  coming true, and it is the argument for the ranked strategy list:

      0 type     role=textbox  name=''                       ordinal=0
      1 type     role=textbox  name=''                       ordinal=1
      2 click    role=button   name='Login'                  ordinal=None
      3 type     role=textbox  name=''                       ordinal=None
      4 click    role=button   name='Search'                 ordinal=None
      5 click    role=link     name='12345 - Testuser Alpha' ordinal=None
      6 extract  role=generic  name=''                       ordinal=2

  An ordinal is a positional bet: step 6 says "the third anonymous generic node". Phase 4 must
  replace it with a relational strategy ("the node inside the cell next to the cell reading Savings
  Balance"), because the ordinal will break the moment the page gains a node above it.

### How the core piece works  (plain English)

The thread runs like this. The agent opens the target, then loops: it asks the browser for one
accessibility snapshot, flattens that into a numbered list of elements with their roles and names
and indentation, and shows the model that text and nothing else, never HTML. The model must reply
with a tool call, and every tool requires a rationale, so an action without a stated reason cannot
be expressed. The loop validates the arguments, turns them into a typed Action, and hands it to
`PolicyGate.dispatch`, which is the only code anywhere allowed to call `Surface.act`. The surface
maps the index the model used back to a live `aria-ref` handle and performs the click or the fill.
Every dispatch is written to run.jsonl through the redactor, with a screenshot beside it. When the
model calls finish, it does not get to end the run: the loop re-observes the page and checks the
declared checkpoint itself, and a checkpoint that does not hold is logged as a rejected completion
while the loop keeps going. Afterwards a separate pass reads run.jsonl back off disk and turns the
successful dispatches into a Capability, converting each recorded element into a role plus name
descriptor, because the refs it used are already stale. Replay then loads that artifact with no model
present at all, re-resolves each descriptor against a fresh observation, requires a unique match,
dispatches through the same gate, and verifies the same checkpoint using the same function the
discovery loop used.

### Decisions logged

- docs/adr/0002-accessibility-tree-over-screenshots.md — perceive through the accessibility tree,
  with the measurements that ruled out screenshots, raw HTML, and the removed Playwright API.
- docs/adr/0003-model-choice-and-free-tier-quota.md — default to `gemini-3.1-flash-lite`, constrain
  `checkpoint.kind` with an enum, and treat quota exhaustion as non-retryable.
- ARCHITECTURE.md gained decisions 25 to 29.

### The loop, in detail

Round 1 problems I found and sent back:

1. **Checkpoint semantics existed twice**, once in loop.py and once in replay/engine.py. The builder
   had left an honest `ponytail:` comment explaining that the import boundary forced the copy, which
   was true but was the wrong fix. If those two ever drift, discovery verifies a goal by one rule and
   replay by another, and the central claim of the project stops being true. Now one pure
   `checkpoint_satisfied(observation, checkpoint)` in models/artifact.py, imported by both.
2. **Screenshots were built but never taken.** `EvidenceLogger.screenshot()` had no caller, so
   evidence/ held only run.jsonl. Now wired per step in discovery and on every hard-failure path in
   replay, guarded so a screenshot failure can never abort a run.
3. **A warning on every model call.** `response.text` was read even when the response held function
   calls, so google-genai printed a non-text-parts warning every turn. Now only read when there are
   no tool calls.
4. **A 40-line traceback on quota exhaustion**, which a reviewer following the README will hit. Now a
   clean one-line message naming the status code and the model, and exit 1.
5. `ReplayResult` was declared and unused, and `bounds` was typed `None` so it could never hold a
   value. Both fixed.

Builder findings worth recording, both of which it flagged rather than papered over:

- **My fact sheet was wrong about one thing.** I told it Gemini accepts `types.Content(role="tool")`.
  The live API rejects it: "Role 'tool' is not supported". Function-response turns have to be sent as
  `role="user"`. The builder verified this with a standalone two-turn test and left the reason in a
  comment. Good catch against an instruction from me.
- **The redactor eats rationale prose.** The model's rationale "Enter the password to log in"
  serializes as `[REDACTED]`, because it contains "password" and the rule redacts the whole string.
  See caveats.

### Caveats / not done

- **The free tier is 20 requests per day, per model, not 15 per minute.** I found this by exhausting
  it mid-verification: `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`, for
  `gemini-3.7-flash` (which `gemini-flash-latest` resolves to). A discovery run costs 8 or 9
  requests, so a free key gets two runs a day per model. The default is now
  `gemini-3.1-flash-lite`, quotas are per model so `GEMINI_MODEL` switches to a fresh budget, and
  the CLI now says so instead of printing a traceback. This needs to be in README.md at Phase 14.
- **The redactor over-redacts.** Step 1's rationale is `[REDACTED]` in both the log and the artifact,
  because the word "password" appears in ordinary prose. That is the coarse rule failing safe, and I
  chose to leave it rather than build Phase 5's redactor early, but it does destroy the R5 "why" on
  that one step. There is a `ponytail:` marker in safety/redact.py naming the fix: scope
  value-redaction to fields carrying literal values, treat free-text rationale as prose subject only
  to the SSN rule and to known literal secrets.
- **Two evidence directories are genuine partial failures**, kept deliberately rather than deleted.
  `discovery-9c0b9811438d` is the run that died on the `role="tool"` rejection, 3 events.
  `discovery-8693d736143d` is my run that died on the 429 daily quota, 8 events. Deleting real failed
  runs is closer to tampering than keeping them; Phase 13 curates what ships.
- **Only the newest discovery run has screenshots.** The three earlier runs predate wiring them, and
  re-running everything to backfill would burn quota for cosmetics. `discovery-3348784c8a88` is the
  complete one.
- **No replay evidence hits an error yet.** The deliverables require a replay that hits an error or
  exceptional state, and there is none here because Phase 2's result contract only has Success and
  HardFailure, with no business-outcome or recovery taxonomy until Phases 6 and 9. Producing a
  contrived crash now would demonstrate nothing. This is an open deliverable, owed by Phase 9 or 13.
- **The artifact is not parameterized.** `inputs: []`, and the member id "12345" is baked into step 3
  as a literal. Replay therefore only reproduces the exact recorded run. Phase 8 turns that into a
  typed input.
- **Every postcondition is null.** Steps carry the field but nothing populates it, so replay only
  verifies the final checkpoint, not each step. Phase 9.
- **The ordinal is a positional bet**, as described in the numbers section. It works today and is
  the reason replay succeeds at all, but 3 of 7 steps depend on it. Phase 4 is where this gets real.
- **PolicyGate enforces nothing yet.** It logs and calls through. The allowlist, the risk
  classification, and the refusal path are Phase 5. The choke point itself is real and enforced.
- **The browser is headed**, so the discovery and replay commands open a visible window and cannot be
  run on a headless box as written. That is required by CLAUDE.md for the Phase 10 handoff.
- `Observation.digest()` has no caller yet. It is specified and is the natural dead-end detector for
  R1's stopping conditions; Phase 3 should use it or it should go.

### Human-review items  (you confirm these)

- [ ] The artifact reads as something a human reviewer and a calling agent could both understand —
  check: `artifacts/look-up-member-12345-and-read-their-current-savings-balance.v1.json`. CLAUDE.md
  says judging this is yours, not mine, so I am printing it, not grading it.
- [ ] The run log tells you what happened and why — check:
  `evidence/discovery-3348784c8a88/run.jsonl`, and the screenshots beside it. Note step 1's rationale
  is `[REDACTED]`; decide whether you want Phase 5 to fix that or whether failing safe is fine.
- [ ] The model default is one you are happy to defend — check:
  `docs/adr/0003-model-choice-and-free-tier-quota.md`. The tradeoff is a weaker model in exchange for
  a reproducible pinned name and a workable quota.
- [ ] CI still green after you push — check: the Actions run. The new tests use fakes and need no key
  and no browser, which is what CI has.

Phase 2 is complete and every machine gate is green. Say "proceed to Phase 3" when you have looked it
over.
