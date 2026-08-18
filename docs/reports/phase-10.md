## Phase 10 Verification Report
Status: COMPLETE. Thirteen of the fourteen definition-of-done items are met, including the three
that required a person at the keyboard, which the user has now driven by hand. The fourteenth
(DoD 5's live half) is recorded as a documented limit of this fixture rather than as a gap: see
below for why it is not reachable and why it was not contrived.

Loop summary: eleven rounds. A (control token, intervention models, store, gate integration) passed
first time. B (operator console, human-action capture) opened with a correctness fix to A: the
one-shot approval lived in an in-memory set on the broker, so it could never have crossed the
process boundary it exists for, and A's test passed only because it granted and consumed on one
object. C wired both execution paths. D fixed an id-redaction hazard C surfaced. E chased a
one-in-three test flake to its real cause. F and G were both found by driving the real system, not
by reading it: F because three declared fields had no writer, G because a risk refusal in replay was
unreachable by approval. H, I, J and K all came from the user driving the real system by hand: H
because human-action capture had never been wired into either path, I because the fix's own test
replicated `escalate()` instead of calling it and so guarded nothing, J because the wiring then
crashed the handoff outright on a navigating click, and K because the severing experiment showed one
line of J's fix did nothing and it was deleted.

Delegation: the builder wrote every source and test file. The main session set the control-transfer
model and the four-state semantics, found the cross-process approval defect before it was built on,
rejected the exception-type reason-code mapping, drove every live run below, and wrote ADR 0015 and
the new ARCHITECTURE.md Phase 9 section that was missing.

### The item that proves the design

DoD 4, run for real and start to finish:

```
$ understudy discover --goal "Open a new savings subaccount for member 12345 with the
    nickname Vacation Fund and an initial deposit of 250" --target http://127.0.0.1:5055/login
status: goal_verified
rounds: 11
rejected turns: 0
intervention: esc529e93616a (resolution: approved)
artifact written: artifacts/open-a-new-savings-subaccount-...-250.v1.json
```

The agent logged in, searched, opened the member, opened the subaccount form, filled every field,
and was refused at the Submit because `/member/12345/subaccount/new` matches `mutating_routes`. It
raised `risky_action_requires_approval`. I approved it through the operator console. The one-shot
approval was consumed (`approval_consumed: true`), the run re-dispatched exactly that click,
reached "Subaccount Opened", and recorded a capability.

**That capability has been impossible to record since Phase 7.** Discovery never auto-approves an
irreversible action, and the recorder only runs on a verified success, so the run always ended in
escalation with nothing to record. Escalation is not a requirement being satisfied here. It is the
only path by which this capability can exist. The recorded artifact is 10 steps ending in the
`RISKY_IRREVERSIBLE` Submit, with `success: text_present "Subaccount Opened"`, and both goal
literals became typed inputs (`member_id: 12345`, `initial_deposit: 250`), with step 5's target
carrying the Phase 9 regex parameterization `:member_id.*` on a freshly recorded flow.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] 1 the gate refuses while a human holds the token — `PolicyDenied` with `rule="control_token"`,
      `Surface.act` never called, asserted for HUMAN and for both transient states
      (PENDING_HANDOFF, PENDING_RESUME), plus the mirror that AUTOMATION allows.
- [x] 2 eight reason codes, eight tests — 10 tests for 8 codes (two codes have both a discovery and
      a replay test). Names:
      `..._stuck_no_progress_is_raised_by_discovery_no_progress_stall`,
      `..._loop_detected_is_raised_by_discovery_repeated_action`,
      `..._locator_unresolved_is_raised_by_discovery_dead_end`,
      `..._max_steps_is_raised_by_discovery_step_budget`,
      `..._risky_action_requires_approval_is_raised_by_discovery_risky_click`,
      `..._risky_action_requires_approval_is_raised_by_replay_subaccount_submit`,
      `..._policy_refused_is_raised_by_discovery_navigation_blocked`,
      `..._policy_refused_is_raised_by_replay_disallowed_role`,
      `..._session_expired_is_raised_by_replay_session_loss`,
      `..._unrecoverable_condition_is_raised_by_replay_app_error`.
- [x] 3 end-to-end session_expired with a manual login — MET, driven by hand by the user against
      intervention `escb4e73c2bb2`. The run escalated at step 2 with `session_expired`, carried its
      masked screenshot, the operator took control, logged in manually in the same live Chromium
      window, returned control, and the run resumed and finished successfully.
- [x] 4 the subaccount capability, approved and recorded — above.
- [~] 5 the dialog policy stands down during a handoff — unit half MET, live half NOT REACHABLE on
      this fixture, and deliberately not contrived. `_DialogPolicy` returns "none" whenever the
      token is not AUTOMATION, checked before the budget is consulted, and "dismiss" with AUTOMATION
      and budget remaining. The AUTOMATIC path is separately proven live: with `?inject=native_dialog`
      a real replay dismissed 3, 1 and 2 dialogs at steps 2, 4 and 5, captured the dialog text, and
      completed in 2.5s. The stand-down itself only fires when a dialog appears WHILE the token is
      HUMAN, and no screen in this fixture both escalates and raises a `window.confirm`, so there is
      no honest way to reach it without building a screen for the purpose. Staging one would prove
      the fixture, not the system. Recorded as a limit with its reason.
- [x] 6 exactly one `run_end` per escalated run — asserted for a resolved escalation and an expired
      one, and confirmed in the real run: `grep -c '"type": "run_end"'
      evidence/discovery-idc6c0778a1d81/run.jsonl` returns `1`.
- [x] 7 `Escalated` is produced, not just declared — live:
      `{"kind": "escalated", "intervention_id": "esc61848bc814", "resolution": "rejected",
      "resumed": false}`, exit 0.
- [x] 8 `human_actions` non-empty — MET on the user's hand-driven handoff: 19 captured actions on
      `escb4e73c2bb2`, in role-and-name terms, covering the click into the username field,
      keystroke-level typing of both fields, both `change` events, the Login click, and the three
      frame navigations that click caused. It FAILED twice before that, and the history matters.
      The cause was not the capture mechanism but its wiring: `install_human_action_capture` had
      exactly one caller in the entire tree and it was a test, so the listener had never been
      injected in a real run and `drain_human_actions` had always returned `[]` in both paths.
      `replay/engine.py` never drained at all, and `operator_app.py` carried a comment asserting in
      writing that it did. Fixed in rounds H and I: capture is installed in `WebSurface.__init__`
      where it cannot be forgotten, the drain lives once in `SessionBroker.escalate()` with a
      discard-then-keep window so the agent's own actions cannot be reported as the human's, and the
      actions are persisted onto the stored resolution inside the store's lock. Still HUMAN REVIEW
      because a non-empty chain on a real hand-driven handoff is the only thing that settles it, and
      that has not happened yet.
- [x] 9 resume skips a step the human already satisfied — plus the `step_skipped_after_handoff`
      event. All three resume branches are now tested: skip, retry-from-top, and escalate-again.
- [x] 10 an expired intervention terminates the run — live:
      `{"kind": "hard_failure", "step_id": 9, "category": "escalation_unresolved",
      "observed": "intervention 'esca9892ccc05' expired with no operator resolution"}` with
      evidence_refs populated, exit 1. The run did not hang.
- [x] 11 the screenshot shown to the operator is masked — it is the file the evidence logger wrote,
      which `redact_screenshot` masked before the PNG bytes hit disk. There is no unmasked original
      on disk at all, which is stronger than masking on the way out.
- [x] 12 the operator page shows which side holds control — MET, confirmed by the user: the banner
      changes colour between states and every screen carries a sentence saying what the current
      state means for the operator right now. The line telling them to use the real, visible
      Chromium window, with the same cookies and the same half-filled form, was judged to be doing
      real work rather than decorating the page.
- [x] 13 ADR — `docs/adr/0015-control-token-and-same-session-handoff.md`.
- [x] 14 this report — `docs/reports/phase-10.md`.

Supporting gates:

- [x] suite — `269 passed, 0 skipped` with the fixture running, run to completion twice
      (124.57s, 122.33s). 55 in `tests/test_phase10.py`. The skip count is reported deliberately:
      a run where the fixture has died reports `244 passed, 25 skipped in 29.76s`, which is a false
      green, and the pass count alone does not distinguish the two.
- [x] no API key — `GEMINI_API_KEY= pytest` exits 0.
- [x] lint and types — `ruff check .` clean, `mypy src/` clean on 38 files.
- [x] invariants — all five, no skips. Invariant 1 was the one at risk, because `replay/` now
      imports `escalation/`: the AST walk reaches 127 modules from `replay/`, including
      `escalation.control` and `escalation.store`, and none is an LLM client or provider SDK.
- [x] no fixed sleeps under `replay/` — the poll wait lives in `escalation/control.py`.

### Human-review items  (all now confirmed by the user)

All three were driven by hand and are recorded as met in the gate above. What the user confirmed:

- DoD 3 and 8, the full manual handoff on `escb4e73c2bb2`: took control, logged in manually in the
  live window, returned control, and the run resumed and completed. 19 human actions captured.
- DoD 12, the control banner: it colour-changes between states and each screen says what the current
  state means for the operator, and the instruction to use the real visible window was judged to be
  carrying its weight.
- DoD 5's live half was examined and found unreachable on this fixture. The user declined to contrive
  it, which is the right call: a screen built solely to raise a confirm during a handoff would prove
  the fixture and nothing else.

### Invariants

```
$ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -q
.....                                                                    [100%]
5 passed
```

### Run-and-report numbers  (reported, NOT gated, never optimized against)

- The subaccount discovery run: 11 rounds, 11 steps executed, 0 rejected turns, 34,363 prompt
  tokens / 388 completion tokens, one escalation, resolved by approval.
- Real interventions produced: 5. One discovery approve, one replay approve, one replay reject, one
  replay expiry, one superseded by the console restart below.
- The approved subaccount replay: Success, 10 steps, 9.1s and 17.2s across two runs.
- Custody chain on a real approved intervention, both processes:
  `automation -> pending_handoff by runner (escalating: risky_action_requires_approval)` then
  `pending_handoff -> automation by operator (approved)`.
- Custody chain on the user's real hand-driven handoff (`esc07f08caffd`, reason `session_expired` at
  step 2), all four states across both processes:
  `automation -> pending_handoff by runner`, `pending_handoff -> human by operator`,
  `human -> pending_resume by operator`, `pending_resume -> automation by runner`.
  That record also carried its masked screenshot (`steps/001_escalation.png`) and a `took_control`
  resolution. Its `human_actions` is empty because it predates the round H and I wiring fix, and it
  is left as recorded rather than edited. It is the run that found the defect.
- Discovery runs this phase: one. Roughly 12 of the free tier's 20 daily requests.

### How the core piece works  (plain English)

A run that gets stuck writes an intervention record to a directory and moves a control token out of
AUTOMATION, then blocks polling that record until an operator decides or the intervention expires.
Because the token is checked by `PolicyGate.dispatch` before anything else, nothing can act while a
human holds it, in either execution path, enforced by the same choke point as every other safety
rule. The operator console is a separate small web process reading the same directory; it shows why
the run stopped, a masked screenshot, and who holds control, and offers either a full handoff or, for
an irreversible action, a one-shot approval. Because Chromium is headed, taking control means using
the browser window the run already has open, so it is genuinely the same session with the same login
and the same half-filled form. On handback the runner does not resume blindly: it re-observes and
skips the step if the human already satisfied its postcondition, retries it if it is still runnable,
and escalates again rather than looping if neither holds.

### Decisions logged

- `docs/adr/0015-control-token-and-same-session-handoff.md` — the four states and why two of them
  are transient, enforcement at the existing choke point, approval as external state rather than a
  caller argument, the same-session claim and its cost, resume-is-not-blind, the dialog stand-down,
  and expiry.
- `ARCHITECTURE.md` gained the Phase 9 section that was missing (decisions 69-76), since REPORT.md
  is assembled from that file.

### Caveats / not done

- **DoD 5's live half is a documented limit, not a gap and not staged.** The stand-down fires only
  when a dialog appears while the token is HUMAN, and no screen in this fixture both escalates and
  raises a `window.confirm`. The unit half is tested, and the automatic dismissal path is proven
  live. Building a screen that exists only to reach the remaining branch would demonstrate the
  fixture rather than the system, so it was not built.
- **The drain crashed the handoff before it worked, and one line of its fix turned out to do nothing.** The
  first wiring fix replaced an empty list with a crash: the human's last action is usually a click
  that navigates, and reading `sessionStorage` through `page.evaluate` in a context a navigation is
  destroying raises. Two things were then measured with the severing experiment rather than assumed:
    - Severing the drain-and-keep call in `escalate()` turns
      `test_live_escalate_drains_and_persists_human_actions_through_the_real_call` red
      (1 failed, 54 passed). The wiring is guarded.
    - Severing the retry inside `drain_human_actions` turns
      `test_drain_human_actions_survives_a_real_navigating_click` red on 3 of 3 runs. The retry is
      guarded, deterministically.
    - Severing the FIRST settle-before-read turned nothing red: 55 passed on 3 of 3 runs. **The
      navigating-click test guards the retry and does not guard the settle.** The method's own
      docstring recorded the measurement explaining why: the first settle-then-read "still raises
      100% of the time" against an unwrapped human click, because Playwright has not yet been told
      the resulting request started, so there is nothing pending for that settle to wait on. The
      retry was therefore the whole fix, and that line has been deleted.

  That negative result is the more valuable of the three. The two positive ones confirmed what was
  expected; the negative one found a line that provably did nothing sitting inside the method named
  after the case it was supposed to handle, which is the same "looks like a protection, is not one"
  shape as everything else in this section. A severing experiment is worth running precisely because
  it can come back negative.
  A drain failure is now also survivable rather than fatal: it is caught one layer up, logged as
  `human_action_drain_failed`, and the run continues, because losing the record of what a human did
  is not a reason to lose the work they did.
- **Human-action capture was never wired, and my own verification missed it. This is the fourth
  instance of one shape in this phase.** The first was an approval kept in memory that could not
  cross the process boundary it existed for. The second was a reason code derived from an exception
  type, which made replay approval unreachable. The third was three declared fields with no writer.
  The fourth was this: a capture mechanism with a passing live test and no production caller. Every
  one of them passed its own test while being unreachable from a real run, and in each case the test
  proved the mechanism rather than the wiring.

  My earlier draft of this report said capture was "built and live-tested against a real browser",
  which was true and misleading. I had verified that the mechanism worked and had a test, not that
  anything called it. Round I closed the general version of the gap: severing
  `escalate()`'s drain-and-keep with a one-line edit used to leave all 52 phase-10 tests passing,
  and now fails one test with "the stored resolution's human_actions must be non-empty". The same
  severing check was run against the persist call and the `handoff_resumed` log; all three are
  guarded. That severing experiment is the cheapest available answer to "does this test guard the
  wiring or only the mechanism", and it is worth applying to the other three defects above and to
  anything similar in later phases.
- **Long-lived local processes serving stale or absent state have now corrupted results three times,
  and the third one is the worst because it reads as a pass.** These are one pattern, not three
  anecdotes.
    1. Phase 9: a Flask fixture started hours earlier served a pre-fix `app.py`, and a retry test
       failed `assert 15 == 2`. Diagnosed by decoding the session cookie, which showed the old
       per-path counter structure the new code cannot produce.
    2. Phase 10: an operator console started before the `transitions` field existed dropped that
       unknown key on read and erased it on write-back, silently emptying the custody chain on every
       intervention it touched, while the one it never touched kept both entries.
    3. Phase 10: the fixture process died mid-session, and the suite reported
       `244 passed, 25 skipped in 29.76s`. Every live test skipped. The builder had already reported
       a "full suite" pass at 198 tests from the same condition.

  The first two are false reds and a silent data loss; the third is the dangerous one. **A skip that
  reads as a pass is a false green**, and nothing in this project detects one. The live-test guard
  checks only that something answers on the port, so it cannot tell a current fixture from a stale
  one, and a `... passed` line with a skip count beside it looks green at a glance and green in a
  grep. Three of this build's verification failures come from that single blind spot.

  The fix is small and is not built: have the fixture expose a build identity (a hash of the module
  it is serving, or an `/admin/version` route) and have the live-test skip guard compare it against
  the working tree, skipping loudly on a mismatch and failing rather than skipping when the target is
  absent. The same argument applies to the intervention record: a version field, refused on mismatch,
  so an older reader cannot silently erase a newer writer's field. Both belong in the same change.

  This sits alongside the four unreachable-mechanism defects above under one heading: a check or a
  component that looks correct while measuring, preserving, or exercising something it is not.
- **The visual evidence is redacted and the structured evidence beside it is not, in the same
  record.** On `escb4e73c2bb2` the masked screenshot blacks out the password field, while
  `human_actions` carries the typed password in plain text on disk. Both halves are behaving exactly
  as built, which is what makes it worth writing down rather than patching quietly.

  The precise cause, measured rather than assumed. The screenshot masker consults element
  sensitivity: perception marks a `type="password"` field `sensitivity="secret"` (the strongest
  structural signal there is, ADR 0008), and `redact_screenshot` masks by that element's bounds. The
  page-level capture listener consults nothing: it records the field's value as a DOM event, with no
  reference to the sensitivity perception already computed for that same element. Redaction does not
  save it either, and not through any defect: R1 redacts values registered via `register_secret`, and
  a value a human types straight into the browser was never a declared parameter for anything to
  register; R3 fires only on a no-whitespace, non-alphabetic literal that ALSO contains a credential
  keyword (`secret`, `password`, `token`, ...), and `hunter2` contains none. Verified directly:
  `Redactor().dumps({"v": "hunter2"})` returns it unchanged, and the same value inside a
  `HumanAction` nested in a resolution is redacted correctly once `register_secret` knows it, so the
  field marking and the tree walk are both fine.

  It is worse in shape than a single leaked field. Capture is keystroke-level, so the secret is on
  disk as its whole typing sequence -- `h`, `hu`, `hun`, through `hunter2`, plus the `change` event,
  eight records for one password. A fix that redacts only a final value would leave the prefixes.

  The fix, named and NOT applied here: apply the same sensitivity classification to a captured
  action's value that Phase 5 already applies to a typed step, and suppress capture of the value
  entirely for an element perception has marked secret, rather than redacting it afterwards. This is
  an R4 exception in the current build ("never persist secrets into artifacts or logs"), so it is not
  merely cosmetic. Phase 13 owns evidence curation and the pre-publication security pass, and this
  belongs on its list by name: `evidence/interventions/escb4e73c2bb2.json` is on disk now and would
  be published unless curated. Section 6 of REPORT.md should state the same limit.

- **`insufficient_funds` is unreachable for a capability that would now earn it.** The subaccount
  flow types into "Initial Deposit", which is exactly the predicate Phase 9 wrote for that outcome,
  but the seed was deleted in Phase 9 because its `balance_check` detector did not exist and an
  outcome naming a missing detector fails validation at load. So a genuinely earnable outcome is
  absent from this artifact. The fix is to write the detector, not to restore the seed.
- **The CLI reports an intervention after the fact, not while it is pending.** Both runners block
  synchronously inside `escalate()`, so the id and the operator URL are printed when the run
  finishes rather than the moment a human is needed. The console is the live view; the CLI is not.
  A watcher thread would fix it and was not built.
- **`context` values pass through the R3 credential-shaped-literal rule.** A `control_token`-rule
  decision reaching `decision_context` would show its rule as `[REDACTED]`, because the string
  contains "token". Not reachable in practice, and it is pre-existing redaction behaviour rather
  than something introduced here.
- **The store's lock has no lease.** A holder that crashes mid-write leaves a lockfile that blocks
  that one intervention id until the bounded acquisition times out and raises. The timeout turns a
  hang into a diagnosable error; it does not remove the underlying limitation.
- **`evidence/` is 57 entries, `artifacts/` is 4, and `evidence/interventions/` holds 8 records.**
  Five runs are mine, driving the definition-of-done table. Four are the user's, driven by hand: the
  one that found the unwired capture, the one that found the drain crash, the successful handoff
  (`escb4e73c2bb2`, `replay-idb0f1bf67e185`), and the native-dialog run that established DoD 5's
  limit (`replay-idb952b1dcc9bc`). The crash run (`replay-id9c958c3dd695`) has no `result.json` at
  all, because the run died before the terminal write, and it is left exactly as recorded rather than
  tidied -- it is the evidence that the crash happened. Phase 13 curates, and must handle
  `escb4e73c2bb2`'s plaintext password before anything here is published.
