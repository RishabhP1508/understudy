# 0010. Diff observations gated on digest equality, and seven stopping conditions

Status: accepted (Phase 7)

## Context

Two separate problems land in the same phase because they share one root cause: a discovery run
that runs forever, or runs on stale information, is not something a single step cap or a token
budget can fix on its own.

The first problem is cost. Every turn re-renders the whole accessibility tree as text and sends
it to the model, even when almost nothing on the page changed since the previous turn. The second
is safety of a different kind: `max_steps` and `timeout_s` (Phase 2) are the only stopping
conditions that exist, and neither says anything about a run that is not making progress, or one
that keeps repeating an identical, ineffective action, or one whose target the model simply cannot
find. All three of those are real, distinct failure shapes, and a caller reading `RunOutcome`
cannot tell them apart from a step cap alone.

## Decision: diff turns, gated on digest equality, never on a schedule

The model addresses every element by the `[index]` it was shown, and that index comes from
`enumerate` over the flat element list `render()` produces. Inserting or removing one element
anywhere in that list shifts every index below it. A diff is only safe to send when the element
list is PROVABLY unchanged, and `Observation.digest()` (Phase 6) already hashes exactly that
structural signature -- role, name, name_source, frame_path, depth -- while deliberately excluding
`value`. An unchanged digest between two turns means the element list, and therefore every index
in it, is identical; the only thing that can have moved is a displayed value.

So: a turn sends a full render on turn 1, on every `full_render_every`-th turn as an unconditional
refresh, and whenever the digest changed since the previous turn. Every other turn sends a
`difflib.unified_diff` (stdlib, no new dependency) of the current render against the previous
turn's full render, labeled explicitly so the model knows it is reading a delta and that lines
absent from the diff still hold. The diff is built from two full render() strings kept
independently of what was actually sent last turn, so a diff-of-a-diff never happens.

**Why gate on the digest and not send a diff on a fixed schedule instead:** sending a diff whenever
convenient, independent of whether the page structure changed, would be the actual bug this
decision exists to prevent. A model told "here is what changed" while secretly holding a stale
index for something that moved would click the wrong element with full confidence -- that is a
correctness defect, not a token-budget tradeoff, and no amount of prompt wording fixes it once the
index itself is wrong.

**The measured token saving is real but not dramatic on this fixture.** Most turns on
`fixtures/legacy_bank` navigate somewhere (a login, a search result, a member detail page), and a
navigation almost always changes the element list, so most turns still legitimately earn a full
render. The diff mechanism pays off most on turns that read a value, retry a rejected action, or
wait through a multi-step form without leaving the page -- exactly the turns where nothing about
the *shape* of the page changed. Reported, not chased: see the Phase 7 verification report for the
actual full-vs-diff turn counts and the observed size reduction against this fixture.

## Decision: seven stopping conditions, not one step cap

`RunStatus` (`agent/loop.py`) is `goal_verified | max_steps | timeout | no_progress |
loop_detected | dead_end | escalation`. The three new conditions share one policy knob,
`stall_limit`, because they answer the same underlying question -- "how many times before this
counts as stuck" -- for three different failure shapes, not three independently-tuned constants
that would drift apart with no real difference between them:

- **`no_progress`**: actions ARE dispatching (the policy gate allows them, the surface executes
  them) but the observation's structure has not moved for `stall_limit` consecutive dispatched
  actions. The fix for this shape is a better prompt, a better checkpoint, or a goal that the
  target application genuinely cannot satisfy the way it was asked.
- **`loop_detected`**: the same `(tool, resolved target descriptor)` pair dispatches
  `stall_limit` times in a row. Deliberately keyed on the descriptor `describe()` produces, never
  on the action's own `node_id` -- a `node_id` is a live accessibility-tree ref that regenerates on
  every snapshot (measured in Phase 2: the same node moved from `f1e1` to `f6e1` across one
  reload), so keying on it would almost never repeat even when the model is plainly clicking the
  same logical element over and over. This condition can fire even when `no_progress` would not:
  a counter that increments on every click changes the observation's *value*, which `digest()`
  deliberately ignores, so the same click can keep "succeeding" by the no-progress measure while
  still being a loop by the resolved-target measure.
- **`dead_end`**: `stall_limit` consecutive turns are REJECTED because the proposed target could
  not be resolved or the tool call's own arguments were invalid (an out-of-range `[index]`,
  a missing field), with no successful dispatch in between. The fix here is a locator or
  perception problem, not a prompting problem -- the model is trying, but nothing it proposes
  actually lands.

`no_progress` and `dead_end` are the pair worth stating plainly as different, because they look
similar from a distance ("the run is not getting anywhere") and need opposite diagnoses:
`no_progress` means dispatch is succeeding and the page is not moving; `dead_end` means dispatch
is not happening at all, because nothing the model proposes ever resolves to a real element.

`escalation` covers two distinct triggers under one status: the model calling the `escalate` tool
directly, and `PolicyGate` raising `EscalationRequired` for a `RISKY_IRREVERSIBLE` action in
discovery mode (Phase 5 deliberately left this uncaught; this phase catches it and turns it into a
clean stopping condition rather than a propagated exception). Both mean the same thing to a
caller -- a human is needed, and Phase 10's handoff is the only path back in -- so they share one
`RunStatus` value rather than forcing every caller to branch on which of the two happened.
`NavigationBlocked` is not folded in here and still propagates uncaught: a session that left the
allowlist is not a state worth resuming reasoning from at all, discovered or escalated.

## Alternatives considered

- **Send a diff on a fixed turn interval, independent of digest.** Rejected: this is the exact
  hazard the digest gate exists to prevent -- a diff sent while the element list changed leaves the
  model holding stale indices.
- **Three separately-tuned stall thresholds.** Rejected: `no_progress`, `loop_detected`, and
  `dead_end` are the same question asked three ways, and three knobs would only invite tuning one
  without the other two, for no benefit any caller has asked for.
- **A single `stuck` status covering all three stall conditions.** Rejected: the whole point is
  that a caller (or a human at escalation time) can look at which one fired and know what kind of
  problem to go fix without reading the transcript first.
