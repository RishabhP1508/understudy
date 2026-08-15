## Phase 4 Verification Report

Status: COMPLETE

Loop summary: 3 rounds. Each round was driven by a defect found through a harder form of
verification than the one before, which is the honest description of how this phase went.

Round 1 built the ranked resolver and passed every test it was given. I then replayed the real Phase 2
artifact against the live server and found round 2's defect. The code-review plugin plus my own
reproduction then found three more, one of which my own round-2 instruction had introduced.

### The headline result

The Phase 2 artifact that stopped replaying, documented at the top of the Phase 3 report, **now
replays end to end**, unmodified.

    $ GEMINI_API_KEY= .venv/Scripts/python.exe -m understudy.cli replay \
        --artifact artifacts/look-up-member-12345-and-read-their-current-savings-balance.v1.json --params '{}'
    { "kind": "success",
      "outputs": { "savings_balance": "$1,204.55" },
      "steps_executed": 7,
      "checkpoint_verified": true }
    exit=0

That artifact still contains its original recorded descriptors (`provenance.transcript_hash
9e190f8669b375668db69a22...`, step 3 still `{"role":"textbox","name":"","ordinal":null}`). Nothing was
re-recorded and nothing was edited. Phase 3's name derivation renamed the login and search fields out
from under it, every name-based strategy correctly finds nothing, and the ranked fallback recovers the
steps positionally. That is the entire thesis of this phase, demonstrated on a real regression rather
than a constructed one.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] ROUND TRIP, describe() then resolve() returns the same element — ran: my own script over EVERY
  element in both captured fixtures — got: **73 tested, 0 failures** — expected: all
- [x] a drifted name falls through and reports its rank — got: the real recorded step-3 descriptor
  resolves at `role_ordinal` rank 5 (details below)
- [x] two controls sharing a name in different frames resolve via scope, not ordinal — got: passing,
  on a CONSTRUCTED observation, because neither fixture contains that case (measured)
- [x] an ambiguous two-candidate strategy is SKIPPED and a LATER strategy wins — got: passing, on a
  CONSTRUCTED duplicate, asserting the winning strategy's index in the enum is greater than
  ROLE_NAME_EXACT's
- [x] CASE 1, the positional bet is gone — ran: resolve on the $1,204.55 node — got:
  `strategy_used=role_name_exact rank=1 candidate_count=1`, a NAMED strategy, not ROLE_ORDINAL —
  expected: named
- [x] CASE 2, the collision — got: **3 candidates by name alone** (cell, iframe, generic), and
  `role=generic` + name is unique at rank 1
- [x] a descriptor matching nothing returns element None WITH per-strategy counts — got: all six
  attempts recorded with counts and reasons
- [x] describe() on an unlabeled input produces a relational hint — got:
  `RelationalHint(kind='row_label', label='Nickname')`
- [x] no CSS is consulted before the name strategies, proven by instrumenting the attempts list —
  got: passing, asserted from the runtime `attempts` order, not by reading source
- [x] TargetDescriptor round-trips through pydantic losslessly — got: passing, including scope tuples
  and the relational hint
- [x] wrong-element bug 1 is dead — ran: `[Edit, Delete, Edit]` — got: `ordinal=2` recorded, resolves
  to node 2 `'Edit'`
- [x] wrong-element bug 2 is dead — ran: `role=button name='Confirm Transfer'` against a page whose
  only button is "Log out" — got: `element=None`, refuses to guess
- [x] replay reports failure when the checkpoint fails — covered by a test on the extracted pure
  helper's false branch
- [x] gates — ran: `pytest -q`, `pytest tests/test_constraints.py -q`, `ruff check .`, `mypy src/` —
  got: **101 passed**, 5 invariants passed with zero skips, `All checks passed!`,
  `Success: no issues found in 30 source files`
- [x] ADR at the next free number — got: `docs/adr/0006-ranked-descriptors-over-selectors.md` (0005
  was highest)
- [x] docs/reports/phase-4.md — this file

### RUN-AND-REPORT: the resolution rank distribution

Over all 73 elements in both captured observations, describe() then resolve():

    rank 1 (ROLE_NAME_EXACT)   25 elements   cell 13, link 3, generic 2, option 2, textbox 2,
                                             iframe 1, combobox 1, button 1
    rank 3 (ROLE_NAME_SCOPED)   7 elements   table 2, rowgroup 2, cell 2, generic 1
    rank 5 (ROLE_ORDINAL)      41 elements   row 16, cell 8, table 6, rowgroup 6, iframe 4, generic 1

The distribution reads badly until you split it by what the elements actually are:

    every element that HAS a name        24 of 24 resolve at rank 1
    every INTERACTIVE element             9 of 9  resolve at rank 1
    everything at rank 5                 anonymous structural scaffolding only

Rank 5 is doing no work that matters. It is 41 unnamed rows, tables, rowgroups and cells, which no
recorded step would ever target, and which no strategy could name because the application gives them
nothing to be named by. Every control a step would actually touch resolves on the strongest signal
available. That is the number I would want a reviewer to see, and it is why I am reporting the split
rather than the headline 41.

### The three defects found after the tests were green

**1. describe() and ROLE_ORDINAL disagreed about which pool the ordinal indexes.** describe() counted
position within the role+NAME pool; the strategy indexed the role-ONLY pool. Reproduced minimally:

    elements:  [button "Edit"(0), button "Delete"(1), button "Edit"(2)]
    describe(element 2)         -> ordinal=1     (index among the two "Edit"s)
    resolve(same, name drifted) -> node 1, "Delete"

A recording that said Edit replays as a click on Delete. In a banking target that is the worst class
of error this system can produce. Both now go through one shared `_role_pool` helper so they cannot
diverge again.

**2. The singleton rescue I asked for in round 2 was too permissive, and that error was mine.** I told
the builder "if exactly one candidate remains, it wins". Consequence, reproduced:

    page:       [button "Log out"]
    descriptor: role=button, name="Confirm Transfer"
    result:     resolved to "Log out" at rank 5, and replay would have clicked it

A recorded name that matches nothing today is a drift signal, and silently clicking the only button of
that role is a confident wrong action, which is worse than failing. The rescue now applies only when
the recorded name was empty, because then position is genuinely all the descriptor ever had. The real
drift case qualifies (Phase 2 recorded `name=""`), so recovery is preserved and the dangerous case now
returns nothing with this reason:

    descriptor recorded name='Confirm Transfer', which matched no element; the sole remaining
    role='button' candidate is not used to rescue a descriptor that once had a meaningful name

**3. The phase's strongest test was green over a broken invariant.** The round-trip test selected
"named or interactive" elements, 12 of 36, and all 12 passed. Over ALL elements the real numbers were
15 failures of 36 and 17 of 37, caused by defect 1. The filter excluded precisely the elements that
failed. The test now runs over every element with no filter: 73 of 73.

**4. Replay reported success when the checkpoint failed.** `engine.py` returned
`Success(checkpoint_verified=False)` and `cli.py` exits non-zero only on `hard_failure`, so a replay
that did not achieve its goal printed a success result and exited 0. It now returns a failure naming
the checkpoint. The checkpoint decides, or it is decoration.

**5. A no-tool-call round would have broken the next model call.** The loop appended an empty model
turn, which becomes `types.Content(role="model", parts=[])`, and the Gemini API rejects zero-part
content. It has never fired, because forced tool calling means the model always calls something, but
at 20 free requests a day a run dying mid-way is expensive. The empty turn is no longer appended.

### Invariants

    $ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -q
    .....                                                                    [100%]
    5 passed

Zero skips. This phase added no LLM import reachable from replay, no second `.act(` call site, and no
new serialization path.

### How the core piece works  (plain English)

A recorded step does not say where its target was, it says what the target is, in several independent
ways at once: its role and accessible name, the two nearest named ancestors that contain it, which
frame it lives in, the caption of the table row it sits in, its position among elements of the same
role, and last, a CSS selector marked brittle. At replay the resolver tries those signals in a fixed
order from strongest to weakest and stops at the first one that identifies exactly one element,
reporting which signal that was. If a signal matches several elements it is skipped rather than
guessed at, because picking the first of three is how automation clicks the wrong row. If nothing
identifies a unique element, replay gets back a list of what each signal tried and how many candidates
it saw, so a failure is debuggable instead of mysterious. The order matters more than any individual
strategy: it means a page can be renamed and still be navigated, and it means the system knows, and
records, that it had to fall back, which is the signal Phase 9 will use to say a page has drifted.

### Decisions logged

- docs/adr/0006-ranked-descriptors-over-selectors.md — the ranked list, why ROLE_ORDINAL keys on role
  alone, the measured name collisions, and why a single recorded signal was proven insufficient.
- ARCHITECTURE.md gained decisions 35 to 39.

### Caveats / not done

- **Three tests were deleted from tests/test_phase2.py**, which the anti-gaming rules would normally
  forbid. They tested the old locator API's exception behaviour (`resolve` raising
  `AmbiguousTarget`/`TargetNotFound`), and this phase deliberately replaced that API with one that
  returns a Resolution and never raises. I checked each property is covered more strongly now: the
  ambiguity case by the constructed-duplicate test that asserts enum ordering, the ordinal case by the
  drift tests, and the not-found case by the per-strategy-counts test. Recorded here because it should
  be visible rather than buried.
- **Two tests were changed from `role="button"` to `role="checkbox"`.** Under the singleton rule a
  lone button legitimately resolves, so those tests stopped exercising the no-match path they exist
  for. `checkbox` appears zero times in that fixture, verified, so the path is genuinely exercised.
  The docstrings record why.
- **Three of the DoD scenarios do not exist in the fixture and are tested on constructed
  observations**: the cross-frame same-name case, the genuine `(role,name)` duplicate, and the login
  page for the drift unit test. Measured: zero duplicate `(role,name)` pairs and zero cross-frame name
  collisions in either capture. Every constructed test says CONSTRUCTED in its docstring. The
  end-to-end replay is the real-data evidence that compensates.
- **DoD 6 could not be met as literally worded.** It asks that ROLE_NAME_EXACT see two candidates for
  "Savings Balance" and decline. It sees one, because role is part of that strategy and the three
  colliding elements have three different roles. The collision is real and is three-way, not two-way,
  by name alone. I have reported what is true rather than engineering a collision that does not
  occur.
- **`dom_fallback` is never populated.** Perception has no CSS to record, so `describe()` leaves it
  None and says so in `notes`. The rung exists, is attempted last, and is proven last by the
  instrumented order test, but it has never resolved anything and cannot until something records a
  selector. It is honest scaffolding, not working code.
- **`name_match` is always "exact".** `normalized` and `regex` are honored by the resolver but
  `describe()` never emits them. They exist for the Phase 12 tenant overlay.
- **`drift_delta` is unwired**, as scoped. Phase 9 consumes it.
- **The loop's `gate.dispatch` is still unguarded.** A Playwright timeout mid-run unwinds the whole
  discovery run and records no capability, which at 20 requests a day is expensive. Fixing it means
  deciding whether a failed dispatch is a rejected turn or a stop condition, which is Phase 7's design
  question, so I left it rather than inventing a policy here.
- **The builder attempted `git status` and `git diff`** while checking it had not modified the
  artifact. The settings hard-block denied both. It then verified by mtime instead and reported the
  attempt. The guard worked as designed; noting it because it is exactly the kind of thing that should
  not pass silently.

### Human-review items  (you confirm these)

- [ ] The rank distribution is one you would defend in an interview — check the split above. The
  argument is that 41 at rank 5 looks alarming until you see they are all anonymous scaffolding and
  that everything nameable resolves at rank 1.
- [ ] The refusal behaviour is the tradeoff you want — check `docs/adr/0006`, decision 38. A
  descriptor whose meaningful name has vanished now fails instead of acting. That means replay breaks
  loudly on a renamed button rather than clicking something else. I believe that is right for a
  banking target; it is your call.
- [ ] The artifact really is untouched — check `provenance.transcript_hash` still reads
  `9e190f8669b375668db69a22...` and step 3 still records `name: ""`.
- [ ] CI green after you push. All 101 tests run without a browser, a network, or an API key.

Phase 4 is complete and every machine gate is green. Say "proceed to Phase 5" when you have looked it
over.
