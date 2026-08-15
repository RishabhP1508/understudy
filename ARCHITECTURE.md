# Architecture

Understudy has two execution paths over one shared artifact. Discovery puts an LLM in an observe,
decide, act loop against a live UI and records what worked. Replay executes that recording with no
model in the decision loop.

This file records each decision and why. It is written ahead of the code, gains a section per phase,
and REPORT.md is assembled from it at the end.

## Structure

1. **Two paths, one contract.** Discovery runs once with a model. Replay runs many times without
   one. Neither path imports the other's internals; both depend on the artifact schema.
   Why: production invocation has to be repeatable and cheap. A model in the replay path makes every
   run a fresh gamble on a task that was already solved, and makes any failure unattributable.

2. **The seam is the Surface protocol.** Everything touching a live UI goes through two methods,
   `observe()` and `act()`. Recorded steps name normalized roles and accessible names, never CSS or
   Playwright APIs.
   Why: this is what lets one artifact and one replay engine reach a second kind of surface. A
   desktop implementation backed by Windows UI Automation would not require the schema or the engine
   to change.

3. **Perception is accessibility-first.** The model reads a rendered accessibility tree, not raw
   HTML.
   Why: legacy targets often have no clean DOM, and the accessibility tree is the one representation
   both a browser and Windows UI Automation expose. It also keeps an observation small enough to fit
   in a prompt without truncation guesswork.

4. **Single process, no scaling infrastructure.** One CLI process per run, plus one local FastAPI
   process for the operator console. No queues, workers, brokers, or containers.
   Why: nothing here needs concurrency, and a queue would add failure modes to demonstrate rather
   than remove. Synchronous is also the honest shape for a headed browser session a human may take
   over mid-run.

5. **The fixture app is a fixture.** `fixtures/legacy_bank/` is a deliberately hostile test target,
   not evaluated code, and its hostility is never reduced to make discovery succeed.
   Why: the locator strategy is the part that gets read hardest, and a sanitized target would prove
   nothing about it.

## Targeting and control

6. **Locators are ranked descriptors, not selectors.** A target records an ordered list of
   strategies: role plus accessible name, then container scope, then relational hints, then ordinal,
   with a CSS fallback last and marked brittle. Resolution requires a unique match. A strategy
   matching two elements is skipped, not resolved to the first.
   Why: first-match-wins is how a replay silently clicks the wrong row. Ranking also produces the
   robustness reasoning the artifact has to carry, and the rank that actually resolved is a drift
   signal across tenants and versions.

7. **`PolicyGate.dispatch` is the only path to an action.** The model proposes actions and never
   holds a browser handle. Discovery and replay call the same gate.
   Why: one choke point is a checkable claim. Guard clauses spread across call sites are not, and
   they decay the moment someone adds a call site.

8. **Business outcomes are not failures.** The result contract separates expected business outcomes
   ("no such member"), recoverable conditions (a slow load, an unexpected dialog), and hard
   failures. Detectors run in that order: business outcomes first, recovery second, checkpoint last.
   Why: a caller that cannot tell "the record does not exist" from "the automation broke" will retry
   a correct answer.

9. **The model never declares its own success.** Terminating the loop runs a deterministic verifier
   against the declared checkpoint.
   Why: self-reported success is the failure that makes a demo look good and production quietly
   wrong.

## Safety and evidence

10. **One serialization path.** Everything written to disk goes through `Redactor`, including
    screenshot bytes, which are masked before the PNG is written.
    Why: an unredacted write path is the one that eventually gets used by accident. Structural
    redaction survives a long build; a habit does not.

11. **Allowlist before action, risk class with it.** Permitted domains, routes, and action types come
    from policy YAML. Actions are classified reversible or irreversible, and the irreversible class
    is handled conservatively.
    Why: the target is a bank fixture. "The agent stayed inside its allowlist" should be a property
    of the code, not of the prompt.

12. **Every action event carries a rationale.** The evidence log records what was done and why, and a
    failure captures a richer signal: screenshot, DOM snapshot, or trace.
    Why: without the why, a run log cannot be debugged afterwards and cannot be handed to a human at
    escalation time.

13. **Handoff happens on the same session.** Escalation pauses automation, transfers a control token,
    and resumes the same headed browser session. Who holds control is explicit state.
    Why: a fresh session loses the login, the wizard position, and the half-filled form, which is
    the state the human was called in to deal with.

## Conventions

14. Python 3.11 as the floor. Pydantic v2 for every type that reaches disk, no dataclasses for
    serialized types. Why: one validation and serialization mechanism, and a schema a reviewer can
    read as code.
15. Playwright driven as a library, launched headed. Why: a human takes over the browser in Phase 10,
    so the window has to be real.
16. typer for the CLI, FastAPI for the operator console. Why: both do the job with no configuration,
    and the console is a mock where polish is wasted.
17. ruff, mypy, pytest, with type hints on public functions. Why: CI runs from a clean checkout with
    no API key and no live services, so the checks have to be static.
18. No fixed sleeps under `src/understudy/replay/`. Explicit waits on conditions only. Why: a sleep
    is a guess about a machine you do not control, and it is how deterministic replay becomes flaky.
19. Config comes from environment variables and policy YAML. Secrets live in a gitignored `.env`,
    with names documented in `.env.example`. Why: this repository is published by hand, and a
    hardcoded key cannot be unpublished.
20. Human-facing prose follows `.claude/skills/human/SKILL.md`. Why: communication is graded, and the
    reader is an engineer comparing submissions side by side.
21. No version control from tooling. Files are written to disk; the user stages, commits, and pushes.
    Why: the repository is the user's to publish and to own.

## Encoded as tests

Five constraints are enforced by `tests/test_constraints.py`, which is fixed and never edited: no LLM
import reachable from `replay/`, no `Surface.act` call site outside `PolicyGate.dispatch`, no
sentinel secret in any serialized output, no transcript field on a serialized capability, and a
`finish` tool schema that requires a checkpoint. Prose does not survive fifteen phases. An AST walk
does. Until the code they guard exists, each test skips with a reason naming the phase that supplies
it. A test skips only when the guarded file is absent, so a module that exists but fails to import
raises an error instead of going quietly dormant.

## Phase 0 decisions

22. The local environment is a `.venv` built with uv against CPython 3.11; CI uses
    `actions/setup-python` at 3.11. Why: the machine default is 3.14, ahead of wheel availability for
    parts of this stack. See `docs/adr/0001-python-311-pin.md`.
23. Packaging is setuptools with `where = ["src"]`, installed editable. Why: it needs four lines and
    no dependency choice.
24. `discover` takes `--goal` and `--target` as separate inputs. `--target` defaults to the policy's
    `entry_point`, and the resolved value is echoed. Why: the target is an input to the capability,
    not deployment config, and echoing it makes the default visible in the run record.

## Phase 2 decisions

25. Perception is one `page.aria_snapshot(mode="ai")` per step, parsed into a flat indexed element
    list. Why: measured against the fixture, a single call crosses the frameset and the depth-2
    iframe and reaches the savings balance, so frame traversal needs no code at all. See
    `docs/adr/0002-accessibility-tree-over-screenshots.md`.
26. The model addresses an element by the `[index]` it was shown; the surface maps that to a live
    `aria-ref` handle. Recorded steps store role plus accessible name plus an ordinal, never a ref.
    Why: refs are regenerated on every snapshot (measured: the same nodes moved from `f1e1` to
    `f6e1` across one reload), so a ref in an artifact would be a guaranteed replay failure. The
    ordinal exists because this app's login and search fields have no accessible name at all, and
    without it replay could not address them. Phase 4 replaces it with the ranked strategy list.
27. Checkpoint semantics live in one pure function, `checkpoint_satisfied(observation, checkpoint)`,
    imported by both the discovery loop and the replay engine. Why: if discovery verified a goal by
    one rule and replay by another, the claim that replay reproduces discovery would be false. One
    definition makes drift impossible rather than unlikely.
28. Every tool in the agent's schema requires a `rationale` argument. Why: R5 makes the "why"
    mandatory, and requiring it at the schema level means an action without a reason cannot be
    expressed, rather than being caught later by review.
29. The default model is `gemini-3.1-flash-lite`, overridable with `GEMINI_MODEL`. Why: measured
    quota, not preference. See `docs/adr/0003-model-choice-and-free-tier-quota.md`.
