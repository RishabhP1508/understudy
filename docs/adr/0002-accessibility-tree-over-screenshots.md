# 0002. Perceive through the accessibility tree, not screenshots or raw HTML

Status: accepted (Phase 2)

## Context

The agent has to understand a screen well enough to act on it, and the target is a deliberately
hostile legacy app: an HTML 4.01 frameset with no `<body>`, tables nested three deep, no ARIA, no
`<label for=>`, form fields named `f1`, `f2`, and `f7`, and a submit control that is an
`<input type="button">` with an inline onclick. The requirements say to bias toward a mechanism that
still works when the surface has no clean DOM. Three options were available: pixels plus a
vision model, raw HTML, or the accessibility tree.

Measurements against the live fixture, taken before choosing:

- `page.aria_snapshot(mode="ai")` called once on the frameset page returns the nav frame, the content
  frame, and the depth-2 `<iframe>` holding the savings balance, in a single tree. Frame traversal
  came free.
- Elements carry a `[ref=fNeM]` handle that is frame-qualified, and `page.locator("aria-ref=f3e26")`
  resolves it back to a real element. A click through that handle on a link inside a frame worked
  from the top-level page, and `inner_text()` on the depth-2 balance node returned `$1,204.55`.
- The two unlabeled login inputs appear as `textbox` with an empty name, sitting next to
  `cell "Username"` and `cell "Password"`. The structure carries the meaning that the markup omits.
- Refs are regenerated per snapshot: the same nodes were `f1e1, f1e2, f2e2` before a reload and
  `f6e1, f6e2, f7e2` after.

## Decision

Perception is the accessibility tree. `Observation` is built by parsing one
`page.aria_snapshot(mode="ai")` per step, and the model is shown a compact indexed text rendering of
it. The model never sees HTML, and it never sees a screenshot.

Refs are used as within-step handles only. The model addresses an element by the index it sees, the
surface maps that to the current ref, and Playwright resolves the ref. Nothing durable ever stores a
ref: recorded steps store a role plus accessible name descriptor, resolved again at replay.

## Tradeoff

The accessibility tree cannot see anything that is purely visual: colour, layout, an icon with no
name, a canvas. If the fixture ever depends on one of those, this design is blind to it. The tree is
also Chromium's computed view rather than the literal document, so a role can differ from the tag a
developer wrote, which is usually helpful and occasionally surprising. The tree is regenerated per
step, so perception costs a round trip; that is acceptable at the step rate an LLM loop runs at.

## Alternatives considered

- **Screenshots plus a vision model.** Rejected. It cannot address elements precisely enough to click
  reliably, it forces coordinate-based actions that break on any reflow, it puts image tokens through
  a free-tier quota, and it makes replay depend on pixels. It would also make the desktop story
  weaker, not stronger.
- **Raw HTML or a DOM dump.** Rejected on the brief's own terms, and on evidence: this fixture's
  member screen is three nested tables of `td3` and `col2` cells, so the HTML is mostly noise, and a
  DOM snapshot of the frameset page contains no content at all because the content lives in child
  frames. It would also tie every recorded step to CSS, which is exactly what the surface-agnostic
  artifact must avoid.
- **Playwright's deprecated `page.accessibility.snapshot()`.** Not available: it has been removed in
  Playwright 1.62.0, which is the installed version. `hasattr(Page, "accessibility")` is `False`.
  Discovering that before writing code rather than after saved a build round.
- **Hand-rolling role and accessible-name computation in injected JavaScript.** Rejected. The
  accessible-name algorithm is genuinely intricate, and reimplementing it would be a large source of
  subtle wrongness for no gain over what the browser already computes.

## Consequence for the artifact

Because refs are unstable across loads, and because the recorded flow must survive a different tenant
or a later version of the vendor app, a step stores what an element IS (role plus accessible name)
and never where it was. That is the same property that lets the schema extend to a desktop surface,
where Windows UI Automation exposes control type and name, the same two things.
