# 0005. Wait for the frame that actually navigated, not the top-level page

Status: accepted (Phase 3)

## Context

`WebSurface.act()` called `page.wait_for_load_state("load")` after every `Click` and `Navigate`.
That call only ever covers the top-level page. This app is a frameset: after the initial `/app`
load, almost every subsequent click navigates a CHILD FRAME (`navframe` or `contentframe`), and the
top-level document never reloads again, so its own `load` state never changes and the wait is a
no-op.

Measured directly: log in, type `12345` into the member search, click Search, observe, and check
for the `"12345 - Testuser Alpha"` result link.

    without any frame-specific wait:  [False, False, False, True, False] -> 1/5
    with the fix below:               10/10 (and separately, 20/20)

Phase 2's discovery runs never hit this, because the 2-10 second LLM round trip between act() and
the next observe() gave the frame time to finish regardless of what act() waited for. Replay has no
model and no pause between steps, so it is exactly where this bites, and it undermines the
determinism the whole replay engine exists to guarantee.

## The race, measured

The obvious fix — call `frame.wait_for_load_state("load")` on the CHILD frame instead of the page —
does not work either. Measured directly against the fixture: right after a click that causes
`contentframe` to submit its own search form, `content_frame.wait_for_load_state("load")` returns in
under 1ms, and `content_frame.url` is still the OLD url. The `framenavigated` protocol event for
that frame has not been delivered to this process yet, so the frame's cached lifecycle state is
stale, and a call that only inspects cached state returns immediately without ever seeing the new
navigation. Only when something else forces a blocking wait (in the probe: an explicit
`page.wait_for_timeout`, which is not something the fix can use) does Playwright's sync API actually
pump the connection and update `content_frame.url` to the post-navigation value.

## Decision

Wrap the click in `page.expect_event("framenavigated", timeout=300)`. `expect_event` starts
listening before the click's own callable runs, so it cannot miss a navigation that starts within
the click's own protocol round trip, and its `__exit__` blocks (pumping the connection) until the
event is actually delivered or the bound elapses. Once it fires, the returned `Frame` object is
guaranteed fresh, so `frame.wait_for_load_state("load")` on it reflects the new document rather than
the old one. The page-level `wait_for_load_state("load")` call stays as a cheap fallback afterward
for the rare click that does cause a top-level navigation.

`expect_navigation()` (the frame/page-scoped legacy API) was rejected for the same job: it has no
predicate to scope to "any frame", and when a click causes NO navigation at all it blocks for its
full default timeout (30s) every single time — a fixed sleep wearing a condition-wait's clothes.
`expect_event("framenavigated", ...)` has the identical shape, and there is no way to prove a
negative ("this click will never navigate anything") without waiting some bound. What changes is the
size of that bound: it is tuned to a real measurement, not a guess.

### Choosing the bound

Measured on this fixture, `framenavigated` arrives 15-30ms after the click that caused it. 300ms
therefore leaves upward of 10x headroom for a slower machine, while a click that never navigates —
a focus click, for instance — pays close to that bound in full (there is nothing to distinguish "not
yet" from "never" faster than that). Measured with the actual fixed code: a sequence of three
non-navigating clicks and three `Type` actions (typing never reaches this code path at all) went
from 3059ms total at a naive 1000ms bound down to 938ms at 300ms — a non-navigating click costs
about 300ms, not zero, and that cost is reported honestly rather than hidden.

## Tradeoff

A non-navigating click is now measurably slower than before this fix (roughly 300ms versus a few ms
under the old, buggy code). That is the real, unavoidable price of correctness here: making the
bound smaller shrinks that cost further but narrows the safety margin against a slower or more
loaded machine, and this fixture measured comfortably inside 30ms even under that pressure. `Type`
and `Select` never call this path at all, since they never navigate anything in this app, so the
cost only applies to `Click`.

This is a Phase 3 fix scoped to the surface's perception/wait mechanism. It is not the deliberate
6-second `slow_load` injection mode (`fixtures/legacy_bank/app.py`), which is a recoverable
condition the caller needs to be told about, not something `act()` should try to silently wait out —
that detection is Phase 9's job (`replay/recovery.py`), and reusing this same 300ms bound for it
would be wrong in the other direction.

## Alternatives considered

- **A persistent `framenavigated` listener registered once, checked without blocking.** Rejected:
  reading a Python-side cached list right after a click has the identical staleness problem as
  reading `frame.url` directly — nothing forces the event to have been delivered yet, so an
  instantaneous check is not more reliable than the naive fix already measured broken.
- **Detect via the `request` event instead of `framenavigated`.** Measured: `request` does fire
  closer to the click (near 0ms in this fixture) for real navigations, but a non-navigating click
  still produces no `request` event, so it has the exact same "must wait a bound to conclude
  nothing happened" cost as `framenavigated`. It would only help the fast path, which is not the
  path this fix is optimizing.
- **`networkidle` instead of `load`.** Rejected: `networkidle` requires 500ms of true quiet, which
  is slower than `load` on the success path for no benefit, and does nothing for the negative-case
  cost this ADR is about.
