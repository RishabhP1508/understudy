## Phase 5 Verification Report
Status: COMPLETE

Loop summary: 2 rounds. Round 1 delivered the whole scope and passed its own checks. Independent
verification found three defects the builder's own checks did not catch, all reproduced before being
sent back: (1) the R3 credential-literal rule was exempted from structural fields by dict key, and
that exemption silently failed inside lists, so the caption "Password" survived at `name` but was
destroyed inside `TargetDescriptor.scope`; (2) `dispatch`'s `finally` treated `about:blank` as an
off-allowlist URL, so a failed connection was reported as `NavigationBlocked` instead of
`ERR_CONNECTION_REFUSED`; (3) `mypy src/` was red on `llm/gemini.py`, which would have made CI red on
push. Round 2 fixed all three, replacing the key exemption with a position-independent shape rule.

Delegation: builder wrote the policy YAML, `safety/risk.py`, `safety/policy.py`, the `safety/redact.py`
rewrite, the navigation guard in `surface/web.py`, the wiring through logger/loop/engine/recorder/cli,
`tests/test_phase5.py`, and both ADRs. Main session set the design decisions before delegating (raise
rather than return on refusal, `Surface.url` on the protocol, screenshot after observe rather than
after act, Pillow over a hand-rolled PNG codec), confirmed against Playwright's source that Chromium
cannot intercept a redirect hop, ran independent verification, reproduced all three defects, and added
the measured over-redaction limit to ADR 0008.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] **1. Invariant 2 passes and does not skip** — ran: `pytest tests/test_constraints.py -rs` — got:
  `5 passed in 0.23s`, with no short-summary skip section at all, so no skip line mentions invariant 2
  or any other — expected: 5 passed, 0 skipped.
  Ran: `grep -rn "\.act(" src/` — got exactly one line:
  `src/understudy/safety/policy.py:271:            result = surface.act(action)` — expected: one call
  site, inside `PolicyGate.dispatch`.

- [x] **2. Invariant 3 against a REAL generated run log and artifact** — drove `agent.loop.run` ->
  `EvidenceLogger` -> `record.build_capability` -> `Redactor`, with `SECRET_SENTINEL_VALUE` typed into
  a `sensitivity="secret"` field and `123-45-6789` inside the model's rationale, then checked the two
  files that actually landed on disk. Got:
  ```
  run status: goal_verified
  SECRET_SENTINEL_VALUE    absent from run.jsonl and artifact in utf-8, url-encoded, base64 forms
  123-45-6789              absent from run.jsonl and artifact in utf-8, url-encoded, base64 forms
  ```
  The run.jsonl line for the type action, as written:
  ```json
  {"type": "policy_decision", "action": {"kind": "type", "node_id": "0", "text": "${param:password}"},
   "context": {"rationale": "Typing the password; the customer's SSN on file is [REDACTED] for reference.", ...}}
  ```

- [x] **3. Off-allowlist navigation is refused** — ran `PolicyGate.dispatch` with
  `Navigate(url="https://evil.example/steal")` — got `PolicyDenied` carrying:
  ```
  allowed=False  rule=allowlist  url=https://evil.example/steal
  reason='https://evil.example/steal' is not within an allowed origin+route
         (allowed_origins=['http://127.0.0.1:5055'],
          allowed_routes=['/login', '/app', '/nav', '/members', '/member/*', '/external'])
  ```
  Test: `tests/test_phase5.py::test_offallowlist_navigation_is_refused`.

- [x] **4. A browser-initiated redirect aborts the run** — ran:
  `pytest tests/test_phase5.py::test_live_external_redirect_is_navigation_blocked -v -rs` — got
  `1 passed in 1.65s`, not skipped, against a real headed Chromium and the live fixture app on
  127.0.0.1:5055 using the Phase 1 `/external` route, which 302s to https://example.com/.

- [x] **5. `classify()` reasons** — ran `classify` against the real policy. Got:
  ```
  'Transfer Funds'   -> RISKY_IRREVERSIBLE
      reason: element name 'Transfer Funds' (name_source='a11y') matches risky_labels entry 'transfer'
  'Search'           -> SAFE_REVERSIBLE
      reason: element name matches no risky_labels entry and the current route is not flagged as mutating
  'Submit'           -> RISKY_IRREVERSIBLE
      reason: the risky_labels heuristic did not match element name 'Submit', but the current route
              '/member/12345/subaccount/new' matches a mutating_routes pattern
  ```
  The third case is the fixture's own control and is the reason the route layer exists.

- [x] **6. `redact_screenshot` verified by pixel comparison** —
  `tests/test_phase5.py::test_redact_screenshot_masks_only_the_flagged_region` builds a 20x20 solid
  PNG, masks bounds `[2,2,6,6]`, and asserts `masked.getpixel((4,4)) != original.getpixel((4,4))`
  (inside the box) and `masked.getpixel((15,15)) == original.getpixel((15,15))` (outside). Passing.

- [x] **7. Password value absent, parameter reference in its place, rationale intact** — got:
  ```json
  {"value": "${param:password}", "rationale": "Enter the password to log in", ...}
  ```
  The Phase 2 regression is gone: that rationale is byte-for-byte intact and asserted as such in
  `test_secret_field_becomes_param_ref_rationale_survives_byte_for_byte`.

- [x] **8. A rationale quoting a real secret IS redacted** — after `register_secret("hunter2")`, got:
  ```json
  {"rationale": "I typed [REDACTED] into the password box"}
  ```
  Test: `test_registered_secret_is_redacted_from_a_rationale_that_quotes_it`.

- [x] **9. Every PolicyDecision, including allows, is in run.jsonl** — over the real generated log:
  `policy_decision events: 2; allowed=2, denied=0`. Every action event carries a `decision` object;
  there is one event type for allows and denials alike, and `record/recorder.py` reads the same event.

- [x] **10. ADR at the next free number** — listed `docs/adr/` first (last was 0006), wrote
  `0007-block-risky-actions-rather-than-confirm.md` and `0008-field-sensitivity-redaction.md`. 0007
  states the limit in its own section: label classification is a heuristic that misses an unlabeled
  irreversible control, named with the measured example (the fixture's submit button is literally
  called "Submit"), which is why route scoping is a second layer.

- [x] **11. This report saved** — `docs/reports/phase-5.md`.

Supporting gates:
```
$ pytest -q                 -> 119 passed
$ ruff check .              -> All checks passed!
$ mypy src/                 -> Success: no issues found in 31 source files
```

### Human-review items  (the user confirms these)

- [ ] The two ADRs defend their decisions well enough to argue in an interview — check:
  `docs/adr/0007-block-risky-actions-rather-than-confirm.md` and `docs/adr/0008-field-sensitivity-redaction.md`
  — what you should see: 0007 argues that confirming inline asks the actor whose action is in question
  to grade its own homework; 0008 documents both directions of the Phase 2 redaction bug and four
  stated limits. Whether the prose is convincing is a judgment call, not a check.
- [ ] CI is green after push — check: the Actions run for this branch — what you should see: ruff,
  mypy and pytest all pass. This depends on a push having happened, so it can never be machine-checked
  from here. The three commands CI runs were executed locally and are green. Note that
  `test_live_external_redirect_is_navigation_blocked` will SKIP in CI, loudly, because CI has no
  Playwright browser and no fixture app; its skip reason names both preconditions.
- [ ] The policy file reads as a policy a reviewer would trust — check: `policies/legacy_bank.yaml`
  — what you should see: every non-obvious entry has a comment saying why, including why `/external`
  is allowed and `/admin/inject` is not.

### Invariants

```
$ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -rs
.....                                                                    [100%]
5 passed in 0.23s
```
All five live, none skipped. Invariant 2 in particular is now enforcing against a gate that actually
refuses, not a pass-through stub, and its skip guard looks for `PolicyGate.dispatch` by name, so the
rename-free restructuring done this phase could not have silenced it.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

No LLM discovery run this phase, so there are no step counts, rejected-turn counts or locator rank
distributions to report. What was measured:

- Test suite: 119 tests, 15 of them Phase 5's, 0 skipped locally.
- One of those 15 drives a real headed Chromium against the live fixture app.
- Verification loop: 2 rounds, 3 defects found by the main session that the builder's own checks
  passed over, 0 defects outstanding.
- Policy surface as configured: 1 allowed origin, 6 allowed routes, 5 allowed action kinds, 8 allowed
  roles, 6 risky labels, 1 mutating route, 3 forbidden text patterns.

### How the core piece works  (plain English)

Every action in the system, in discovery and in replay alike, goes through one method,
`PolicyGate.dispatch`, and that method is the only place in the codebase that calls `Surface.act`, which
a test enforces by walking the AST rather than by grepping. Dispatch runs five checks in a fixed order
before it will touch the browser: is this URL inside the allowed origins and routes, is this kind of
action allowed at all, is the target's role allowed, does the typed text match a forbidden pattern, and
is this action reversible. That last question is answered by two independent layers, because one is not
enough: a label heuristic reads the control's own name, and a route rule flags whole paths as mutating,
which is what catches the fixture's irreversible submit button whose name is just "Submit". An
irreversible action is refused outright rather than confirmed, and refusal raises rather than returning
a value, so no caller can ignore it by accident. Separately, the browser itself is watched, because the
agent is not the only way to leave the allowlist: a route handler blocks an off-allowlist navigation
request, and because Chromium will not let anything intercept a redirect hop, a second listener watches
requests so a server-side 302 to another origin is detected and aborts the run. Nothing reaches disk
except through the Redactor, including screenshot pixels, which are masked before the PNG bytes are
written; and what gets redacted is decided by the field a value was typed into, not by scanning prose
for words like "password".

### Decisions logged

- `docs/adr/0007-block-risky-actions-rather-than-confirm.md` — refuse and escalate an irreversible
  action rather than confirming it inline, with two independent risk layers because the label
  heuristic demonstrably misses this fixture's own submit control.
- `docs/adr/0008-field-sensitivity-redaction.md` — redaction is driven by the field a value targets,
  never by keyword matching on prose; four rules, lazy bounds resolution, Pillow, and four stated
  limits.
- `ARCHITECTURE.md` — new "Phase 5 decisions" section, items 40 to 49.

### Caveats / not done

- **R3 over-redacts an ordinary identifier containing a credential word.** Measured on a real run: a
  checkpoint value `DONE_TOKEN` and a URL path `/secret-flow` are both blanked, because each is
  whitespace-free, non-alphabetic and contains a credential token. They are structurally
  indistinguishable from `SECRET_SENTINEL_VALUE`, so no shape rule separates them, and the key-based
  rule that could was removed for being position-dependent. The real fixture is unaffected: no route
  or checkpoint value in the policy or the recorded artifact contains a credential word. The failure
  mode is loud, not silent, since a blanked checkpoint cannot match and replay returns a HardFailure
  naming it. Documented as a stated limit in ADR 0008; the principled fix is schema-level marking of
  value-carrying versus structural fields, which belongs with Phase 8's artifact schema work.
- **The carry-forward item from Phase 4 is resolved, but only half of it is settled.** `agent/loop.py`
  now catches `PolicyDenied`, logs it, counts a rejected turn and feeds the refusal back to the model.
  `EscalationRequired` and `NavigationBlocked` deliberately propagate and end the run. Whether a
  denial should instead be a stop condition is Phase 7's call and is left open on purpose, with a
  comment at the call site saying so.
- **`llm/gemini.py` was touched outside this phase's scope.** `mypy src/` was failing there on a list
  invariance mismatch against google-genai 2.18.1. It is not Phase 5 code, but CI runs `mypy src/`
  from a clean checkout that resolves the same version, so leaving it would have handed over a red
  push. Fixed with a `cast` to the SDK's own declared parameter type, no runtime change, no blanket
  ignore.
- **Screenshot timing changed.** Screenshots are now taken after `observe()` and before the action,
  using that same observation, because a mask positioned from a stale observation lands in the wrong
  place, which is a leak rather than a cosmetic bug. The cost is one extra `observe()` for the
  bootstrap screenshot, and the final post-action state of a discovery run is no longer captured as
  its own frame.
- **The attribute-read budget is now spent differently.** `_resolve_attr_names` used to skip any
  element that already had a name; it can no longer do that, because this app's password field gets
  its name from the row-label rule before that method runs, and skipping it would lose the
  `type="password"` signal on exactly the field that matters most. The cap of 30 is unchanged, so on a
  page with many interactive elements fewer unnamed ones now get an attribute-derived name.
- **`evidence/` has accumulated run directories from live verification.** These are real system output
  and are left in place per the never-author-evidence rule. Curating them is Phase 13's job.
- **Multi-tenant policy and the operator console are not in this phase.** `policies/legacy_bank_tenant_b.yaml`
  (Phase 12) and the escalation path that `EscalationRequired` is waiting for (Phase 10) do not exist yet,
  so a risky action in discovery currently ends the run rather than pausing it for a human.
