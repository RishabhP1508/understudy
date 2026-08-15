## Phase 3 Verification Report

Status: COMPLETE

Loop summary: 3 rounds, plus a probe phase before delegating.

The probe found the trap this phase turns on. The obvious implementation of "take the caption from
the neighbouring cell" is a backwards scan for the nearest preceding named cell, and measured against
the live fixture it derives `Savings` for the account-type dropdown instead of `Account Type`. The
reason is specific: the `<td>` that contains the `<select>` gets its own accessible name computed
from the selected option's text, so it sits between the caption and the control in document order.
The rule has to be structural, climbing to the control's own containing cell first. I put that
measurement in ADR 0004 and handed the builder the acceptance targets rather than the algorithm.

Known state: artifacts/look-up-member-12345-....v1.json no longer replays. It recorded the login
field as role='textbox' name='', and Phase 3's name derivation now perceives that field as
name='Username'. The app did not change; our reading of it did. Verified by five identical
hard_failure results on step 0 against a live server. Not re-recorded, because free-tier quota is
20 requests per day per model and Phases 7 and 8 both run discovery. The schema fix is
provenance.perception_version, owed by Phase 8, with matching failure categories owed by Phase 9.

Round 1 built it and it worked. Round 2 fixed a latent bug I found by reading the climb, and replaced
a simulated navigation in a test with a real one. Round 3 fixed a determinism defect I found by
running my own verification twice and getting different answers.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] observation reaches inside the content frame, not just the frameset — ran: my own probe driving
  the real UI — got: member detail returns **36 elements, 33 of them carrying a frame_path** —
  expected: elements from inside the frames
- [x] the balance node carries a frame_path of depth 2 — got:
  `frame_path=['contentframe', '/member/12345/balance']` — expected: 2 segments
- [x] **THE POINT OF THE PHASE: the balance node is no longer nameless** — ran: live probe and the
  captured fixture — got: `name='Savings Balance'`, `name_source='row_label'`, where Phase 2 could
  only reach it as `role=generic, name="", ordinal=2` — expected: a non-empty derived name with its
  source recorded
- [x] at least three elements have a name_source other than the a11y tree — got **eight**, all
  `row_label` (listed below) — expected: >= 3
- [x] render produces no HTML and respects the cap — ran: `render(max_elements=60)` on the captured
  observation — got: 15 lines shown below, `contains HTML angle brackets: False`, and a truncation
  notice when the cap bites — expected: indexed text, no markup
- [x] digest is identical on an unchanged page and differs after a navigation, asserted in a test —
  ran: `pytest tests/test_phase3.py` — got: passing, comparing **two real captured observations**
  (`d6599e5cee61` vs `489f2750d1c1`) — expected: both assertions, in a test
- [x] every Surface method has a DesktopSurface counterpart that raises, naming a specific UIA API —
  ran: printed the docstring (excerpted below) — got: `observe` and `act` both raising, with named
  APIs per concept — expected: concrete, not vague
- [x] the captured observation fixture exists and is non-trivial — got:
  `observation_member_detail.json` 36 elements, `observation_subaccount_form.json` 37 elements, both
  captured by driving the live app — expected: real, non-trivial
- [x] `mypy src/understudy/surface/` exits 0 — got: `Success: no issues found in 5 source files`
- [x] `mypy src/` and `ruff check .` — got: `Success: no issues found in 30 source files`,
  `All checks passed!`
- [x] whole suite and invariants — ran: `pytest -q` and `pytest tests/test_constraints.py -q` — got:
  `13 passed`, and `5 passed` with zero skips
- [x] an ADR on name derivation exists at the next free number — got:
  `docs/adr/0004-name-derivation-for-unlabeled-controls.md` (0003 was the highest existing), plus
  `0005` for the navigation-wait fix
- [x] docs/reports/phase-3.md — this file

### The derived names, measured live

Eight elements across the app now carry a name the application never provided. All eight resolved
through `row_label`; `column_header` and `attr_name` exist as lower rungs and never fired, because
every caption in this fixture is a preceding cell in the same row.

    screen              role       derived name        name_source   frame_path depth
    login               textbox    'Username'          row_label     0
    login               textbox    'Password'          row_label     0
    member search       textbox    'Member ID'         row_label     1
    member detail       iframe     'Savings Balance'   row_label     1
    member detail       generic    'Savings Balance'   row_label     2   <- the balance itself
    subaccount form     combobox   'Account Type'      row_label     1   <- the one the naive rule got wrong
    subaccount form     textbox    'Nickname'          row_label     1
    subaccount form     textbox    'Initial Deposit'   row_label     1

The trap is visible in the rendered output itself. On the subaccount form the container cell really
is misnamed, and the control beside it is still correct:

    [11]                       cell "Account Type"
    [12]                       cell "Savings"
    [13]                         combobox "Account Type"

### render(), first 15 lines

    URL: http://127.0.0.1:5055/app
    [0] generic
    [1]   iframe
    [2]     table
    [3]       rowgroup
    [4]         row
    [5]           cell "Legacy Bank"
    [6]         row
    [7]           cell
    [8]             link "Member Search"
    [9]   iframe
    [10]     generic
    [11]       table
    [12]         rowgroup
    [13]           row

Note line 0: the URL is `/app`, the frameset, while the content being rendered comes from two frames
below it. That is the whole point of the perception model.

### A determinism defect found by running verification twice

My own verification probe passed on one run and failed on the next. Rather than re-run it, I
diagnosed it, and it turned out to be the most important finding of the phase.

`act()` waited on `page.wait_for_load_state("load")` after a click. That covers the top-level page
only. In a frameset the top document never reloads after the initial `/app` load, so for the child
frame navigations that make up nearly every interaction in this app, the wait was a no-op. Measured:

    without a frame-specific wait:  [False, False, False, True, False]  -> 1/5
    with the fix, measured by me:   10/10 in 11.8s for ten full login-and-search sequences

Phase 2's three green discovery runs hid this completely, because the 2 to 10 second model round trip
between acting and observing gave every frame ample time to settle. Replay has no model and no pause,
which is exactly where it would have bitten, and it would have looked like random flakiness six
phases later.

The builder's diagnosis of why the obvious fix also fails is worth keeping: calling
`frame.wait_for_load_state("load")` on the child frame returns in under a millisecond with the frame
still reporting its OLD url, because Playwright's sync API only pumps its connection during a
blocking wait, so cached lifecycle state is stale. Only entering a real blocking wait on the event
itself closes the race. The fix wraps the click in `page.expect_event("framenavigated", timeout=300)`,
which starts listening before the click runs.

The honest cost, measured and not hidden: a click that navigates is fast, and a click that navigates
NOTHING now pays about 300ms, because there is no way to prove a negative faster than some bound. The
bound is 300ms against a measured 15 to 30ms real navigation latency. `Type`, `Select`, and `Key`
never touch this path.

### The boundary of that fix, also measured

The ADR claims the 300ms bound deliberately does not cover the fixture's 6-second `slow_load`
injection, and that detecting it belongs to Phase 9. I tested that claim rather than accepting it:

    click returned after 0.30s (bound is 0.30s)
    result link visible immediately after act() returned: False

So under `slow_load`, `act()` returns before the response has committed and the caller sees stale
content. This is correct behaviour for a perception layer, and it is a precise specification for
Phase 9: a slow load is a recoverable condition the caller must be told about, not something `act()`
should silently absorb, and reusing a 300ms bound to detect it would be wrong in the other direction.

### The desktop seam

`surface/desktop_stub.py` maps every concept to a named UIA API. Excerpt:

    observe()   -> IUIAutomation::CreateTreeWalker (ControlViewWalker), driven explicitly with
                   GetFirstChild/GetNextSibling. No single call returns the whole tree the way
                   page.aria_snapshot does, so this is real recursive code the web surface never
                   had to write.
    role        -> AutomationElement.Current.ControlType (UIA_ButtonControlTypeId, ...)
    name        -> AutomationElement.Current.Name
    value       -> ValuePattern.Current.Value (IUIAutomationValuePattern), probed per element
    bounds      -> AutomationElement.Current.BoundingRectangle (cleaner than the web case)
    Click       -> InvokePattern.Invoke(), falling back to
                   LegacyIAccessiblePattern.DoDefaultAction() for controls that expose no
                   InvokePattern, and to a synthesized click at the BoundingRectangle centre last
    Type        -> ValuePattern.SetValue()
    Select      -> SelectionItemPattern.Select() on the item, with ExpandCollapsePattern to open
    frame_path  -> the Window/Pane containment hierarchy

It is also explicit about what does NOT map. The row-label rule has a *richer* desktop analogue where
`GridPattern.GetItem(row, col)` and `TableItemPattern.GetRowHeaderItems()` exist, but the fixture's
actual failure mode, a legacy app drawing its own grid with no grid pattern at all, leaves UIA
exposing flat `Text` and `Edit` siblings with no row or cell roles to climb. There the derivation
would fall back to bounding-rectangle proximity, a coordinate heuristic that would be marked brittle
in the artifact the same way the CSS fallback already is. That paragraph is the source material for
REPORT.md section 4.

### Invariants

    $ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -q
    .....                                                                    [100%]
    5 passed

Zero skips, unchanged. This phase added no import that reaches the LLM from replay, added no second
`.act(` call site, and changed no serialization path.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

- No model ran this phase; perception needs no LLM. No discovery steps or token usage to report.
- Perception: 36 elements on member detail, 33 with a frame_path, tree depth 16 at the balance node.
- Name derivation: 8 derived names, 8 of 8 via `row_label`, 0 via `column_header`, 0 via `attr_name`.
  The `attr_name` rung costs a round trip per element and never fired on this app.
- Navigation reliability: 1/5 before the fix, 10/10 after, measured by me independently.
- Non-navigating click cost: about 300ms, up from a few ms, as the price of that reliability.

### How the core piece works  (plain English)

Perception still takes exactly one accessibility snapshot per step, because that single call already
returns the frameset, both child frames, and the nested iframe as one tree. What this phase added is
what happens to that text afterwards. The parser now reconstructs the tree from the indentation, so
every element knows its parent, and three things get derived from that shape. First, an element's
frame_path: walk up to the iframe ancestors and resolve each one to a real browser frame, so the
balance node records that it lives in `contentframe` and then in the balance frame. Second, its
nearest few ancestors, so a reader can see the context an element sits in. Third, and the reason the
phase exists, its name: if the browser computed no accessible name, climb from the element to the
table cell that contains it, then take the caption from the nearest preceding cell in the same row.
That climb crosses the iframe boundary without any special handling, because the snapshot already
threaded the frames into one tree, which is how a node buried 16 levels deep inside a nested frame
gets called `Savings Balance` instead of "the third anonymous node". Whichever rung produced the name
is stored on the element, so a derived name is never mistaken for one the application actually
provided.

### Decisions logged

- docs/adr/0004-name-derivation-for-unlabeled-controls.md — the structural row rule, the measured
  naive-versus-structural comparison, why `<label for=>` is not implemented as a separate strategy
  (the browser has already folded it into the accessible name, so it would be unreachable code), and
  the "known issue, owed by Phase 5" record of the redaction defect.
- docs/adr/0005-child-frame-navigation-wait.md — the navigation race, why the obvious frame-level fix
  also fails, and how the 300ms bound was measured rather than guessed. Drafted by the builder during
  the fix; I verified its claims independently, including the slow_load boundary it asserts.
- ARCHITECTURE.md gained decisions 30 to 34.

### Caveats / not done

- **`slow_load` defeats the navigation wait, by design and now by measurement.** `act()` returns at
  the 300ms bound with stale content. Phase 9 must detect it as a recoverable condition. Recorded
  here and in ADR 0005 so it is not rediscovered as a mystery.
- **A non-navigating click costs about 300ms.** Unavoidable without a way to prove a negative
  instantly. If it ever matters, the bound is one constant.
- **`column_header` and `attr_name` never fire on this fixture.** They are lower rungs of the ADR 0004
  ladder that this app does not exercise. They are not dead code in the general case, but on this
  target their coverage is zero, and I am reporting that rather than implying the whole ladder was
  validated. Only `row_label` has been proven against a real screen.
- **Two elements now share the name "Savings Balance"**, the iframe and the generic node inside it,
  because both climb to the same caption. They differ by role so resolution stays unique today, but
  it is the kind of collision Phase 4's ranked strategies need to handle deliberately.
- **`bounds` is still None.** Phase 5 turns on `aria_snapshot(boxes=True)` for screenshot masking.
- **The captured fixtures record `url: /app`** for both pages, because the frameset URL does not
  change when a child frame navigates. That is correct and worth knowing before someone reads the
  fixture and thinks the capture is wrong.
- **Perception cost has gone up.** Each observation now resolves every unique iframe ref to a real
  frame object, one round trip per iframe. Three frames here, so it is cheap, but it scales with the
  number of frames, not the number of elements.
- ADR 0005 was drafted by the builder rather than the main session, which is a deviation from the
  model routing in CLAUDE.md. I reviewed it line by line and independently reproduced its two central
  measurements before accepting it.

### Human-review items  (you confirm these)

- [ ] The derived names are ones you would defend as correct — check: the table above, and
  `tests/fixtures/observation_member_detail.json`. The question to ask is whether "Savings Balance"
  is the right name for the node holding `$1,204.55`, given the caption is in the neighbouring cell.
- [ ] The desktop mapping is specific enough to survive a reviewer who knows UIA — check:
  `src/understudy/surface/desktop_stub.py`. This is the raw material for REPORT.md section 4, and it
  is the main evidence that the Surface seam is real rather than asserted.
- [ ] The 300ms navigation bound is a tradeoff you accept — check:
  `docs/adr/0005-child-frame-navigation-wait.md`.
- [ ] CI still green after you push. The two new fixtures are JSON read from disk, and no test in this
  phase needs a browser or a network.

Phase 3 is complete and every machine gate is green. Say "proceed to Phase 4" when you have looked it
over.
