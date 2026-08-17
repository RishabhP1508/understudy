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

## Phase 3 decisions

30. An unlabeled control gets its name from the surrounding table structure, and the strategy that
    produced it is recorded as `name_source`. Why: this app has no `<label for=>` at all, so without
    derivation the only way to address a field is its position. The rule climbs to the control's own
    containing cell before reading the preceding sibling cell, because a backwards text scan
    measurably returns "Savings" for the account-type dropdown instead of "Account Type". See
    `docs/adr/0004-name-derivation-for-unlabeled-controls.md`.
31. A derived name is always distinguishable from an authored one, because `name_source` travels
    with the element. Why: an inferred name is weaker evidence than one the application actually
    provides, and a reviewer reading an artifact should be able to see which is which rather than
    having to trust all names equally.
32. Name derivation is skipped for structural container roles (`cell`, `row`, `rowgroup`, `table`).
    Why: in a layout of tables nested three deep, a nameless container would otherwise inherit the
    caption of an unrelated outer row.
33. `Click` waits for the frame that actually navigated, not the top-level page. Why: measured, the
    old top-level wait produced the correct result 1 time in 5, because in a frameset the top
    document never reloads. See `docs/adr/0005-child-frame-navigation-wait.md`.
34. `DesktopSurface` exists as a documented, non-functional seam. Why: the claim that the artifact is
    surface-agnostic should be checkable against a second concrete surface. The module maps every
    Surface concept to a named UIA API and states plainly where the mapping does not hold, which is
    the honest form of that claim.

## Phase 4 decisions

35. A recorded target carries several independent signals and resolution walks them in a fixed
    order, reporting which one won. Why: a step with one way to find its target is one perception
    change away from worthless, which is not hypothetical here. See
    `docs/adr/0006-ranked-descriptors-over-selectors.md`.
36. A strategy matching more than one element is skipped, never resolved to the first match, and a
    total failure returns the per-strategy candidate counts. Why: first-match-wins is how replay
    silently acts on the wrong row, and a failure that says "role_name_exact saw 0, role_ordinal saw
    3" is debuggable where "not found" is not.
37. `describe()` and `ROLE_ORDINAL` compute the ordinal through one shared pool helper. Why: they
    were briefly allowed to disagree, and a recorded ordinal then meant something different at
    replay than at record time. Measured: a descriptor for the second "Edit" button resolved to
    "Delete". Two definitions of the same index is a wrong-element bug waiting to happen.
38. A descriptor that recorded a MEANINGFUL name and now matches nothing is not rescued by falling
    back to the only element of that role. Why: that is a confident wrong action, which is worse
    than a reported failure. Measured: a descriptor for "Confirm Transfer" resolved to "Log out".
    The positional rescue applies only when the recorded name was empty, because then position is
    all the descriptor ever had.
39. Replay returns a failure when the success checkpoint does not hold. Why: it previously returned
    Success with `checkpoint_verified: false` and exit code 0, so a replay that did not achieve its
    goal reported success. The checkpoint is what decides, or it is decoration.

## Phase 5 decisions

40. Policy is a typed Pydantic model, loaded by one function (`safety/policy.load_policy`), and
    the stub `config.load_policy` is deleted. Why: `replay/engine.py` must not import
    `understudy.config` (invariant 1's neighbourhood), and a policy read as a plain dict would
    have pushed every field name and default back into whichever module happened to read it
    first, instead of into one schema a reviewer can read as code.
41. `PolicyGate.dispatch` raises on refusal instead of returning a sentinel. Why: its return type
    is `str | None` (the `ReadText` result), so a sentinel refusal value would be silently
    ignorable at the one call site that matters most. Three sibling exception types --
    `PolicyDenied`, `EscalationRequired`, `NavigationBlocked` -- carry the decision or the
    offending URLs, and are siblings rather than a hierarchy because discovery, replay, and the
    CLI handle each of the three differently. See
    `docs/adr/0007-block-risky-actions-rather-than-confirm.md`.
42. Risk classification runs two independent layers, label then route, first match wins, and the
    route layer exists because the label layer is a measured heuristic with a demonstrated blind
    spot on this fixture's own "Submit" button. See `docs/adr/0007`.
43. Sensitivity is a field on `UIElement`, set during perception from two structural/data-driven
    signals (`type="password"`, then a policy pattern match), never inferred from a rationale
    string. Why: prose is for a human to read; deciding what is secret by scanning prose for a
    keyword is the exact rule that redacted "Enter the password to log in" in Phase 2. See
    `docs/adr/0008-field-sensitivity-redaction.md`.
44. The web navigation guard is two handlers, not one: `page.route` blocks an initial off-allowlist
    navigation request before it starts, and `page.on("request")` separately records a redirect hop
    Chromium never surfaces as an interceptable `Route` at all (verified in Playwright's own
    source and test suite). Why: either handler alone misses one of the two ways this app's own
    `/external` route can leave the allowlist -- a direct navigation, and a server-side 302 -- and
    the second one is exactly how a real escape would happen in practice.
45. Redaction gained a fourth rule (a bare, no-whitespace, NOT-purely-alphabetic credential-shaped
    literal is redacted whole) and a field-level mechanism (a dict carrying its own
    `"sensitivity"` marks which of its keys to replace). Why: applied without the alphabetic
    exclusion, the same literal rule that catches `SECRET_SENTINEL_VALUE` also nukes this
    fixture's own password field's caption ("Password" is a bare string containing the token
    "password" too), corrupting a `TargetDescriptor` for no safety benefit -- a caption is not a
    secret. The exclusion is a property of the string's own shape, not of which dict key or list
    position it sits in, on purpose: a key-keyed exemption was tried first and broke the moment
    the same caption appeared inside `TargetDescriptor.scope`, a list with no key at all. See
    `docs/adr/0008`.
46. A sensitive element's live bounding box is resolved lazily, only for elements a redaction check
    has already flagged, never for a whole observation. Why: perception stays cheap by default, and
    the cost of a `bounding_box()` round trip is paid only where a screenshot is actually about to
    be masked.
47. A screenshot is taken right after `observe()` and before the action it will accompany, using
    that same observation, in both discovery and replay's failure path. Why: masking positions a
    box using an observation's element bounds, and a stale observation describes pixels from a
    different moment than the ones the screenshot shows -- painting a mask in the wrong place is a
    leak, not a cosmetic bug.
48. `Capability` carries a `status` (`"draft" | "approved"`). A `RISKY_IRREVERSIBLE` step only
    replays when the artifact's own status is `"approved"` AND the caller separately passes
    `allow_risky=True`. Why: approval travels with the artifact a human actually reviewed, and a
    separate per-invocation flag means approving a capability once does not silently arm every
    future replay of it forever.
49. The evidence log has one event shape for a dispatched action, `policy_decision`, carrying an
    `allowed` field, not two event names for "did it" and "refused it." Why: a refusal and an
    action are the same decision with a different outcome, and `record/recorder.py` needs exactly
    one predicate (`decision.allowed is True`) to know which events became `Step`s, not two event
    names to keep in sync.

## Phase 6 decisions

50. Every line of `run.jsonl` is built through one Pydantic model, `RunEvent`
    (`evidence/logger.py`), not assembled as a bare dict per call site. Why: "every line
    validates against the event schema" is otherwise a claim someone has to check by reading
    fifteen call sites; built through one model, it is true by construction. A `type == "act"`
    event additionally cannot be constructed at all without a real, non-redacted `rationale` --
    R5 enforced at the schema level, not by convention.
51. The event PolicyGate.dispatch logs is renamed from `policy_decision` to `act`, and is now
    logged AFTER `surface.act` runs (in `except`/`else`, before `finally`'s own navigation check
    can raise), not before. Why: the one event for a dispatched action can then carry its own
    result (`act_result`), which is stronger evidence of what happened than logging the intent to
    act and hoping nothing went wrong afterward.
52. The result contract is four terminal kinds (`Success`, `BusinessOutcome`, `HardFailure`,
    `Escalated`) on a `kind` discriminator, with `HardFailure.category` a ten-value StrEnum and a
    recovered condition logged as a `run.jsonl` event, never a fifth kind. Why, and the full
    ten-category reasoning: `docs/adr/0009-result-contract-and-failure-taxonomy.md`.
53. `Provenance.perception_version` and the module constant `PERCEPTION_VERSION` exist so a
    locator failure can be CLASSIFIED (stale perception vs. a genuinely unresolved target), never
    so a version mismatch can gate replay before it starts. Why: the one artifact in `artifacts/`
    predates the field, still replays successfully end to end, and must keep doing so -- a
    pre-flight gate would refuse runs that would have succeeded.
54. Screenshot pairs, not singles: `steps/NNN_before.png` masks the observation the decision was
    made from; `steps/NNN_after.png` masks a FRESH observation taken once the action has run, in
    both discovery and replay, on every step, not only on failure. Why: the action just changed
    the page, so masking `after` from the pre-action observation would position a box over pixels
    that no longer show what it thinks they show -- a leak, not a cosmetic bug. The cost is one
    extra `observe()` per step, paid and noted in code rather than avoided by reusing a stale one.
55. A discovery run writes `transcript.jsonl` incrementally, one redacted line per model turn, so
    a crashed run still has every turn it completed. Why: the alternative (buffer the transcript
    in memory, write it once at the end) loses the R8 evidence trail on exactly the runs most
    worth debugging -- the ones that did not finish.

## Phase 7 decisions

56. Provider selection is one `build_llm(settings) -> LLMClient` behind the existing protocol,
    with a registry of one entry (`{"gemini": GeminiClient}`), chosen by `LLM_PROVIDER`
    (default `"gemini"`). Why: one real implementation behind the protocol is the seam the brief
    actually asks for; a second, never-exercised provider client would be gold-plating with no
    test that ever calls it live.
57. A discovery turn sends the model a diff of `render()` output, built with
    `difflib.unified_diff` over two full renders, ONLY when `Observation.digest()` is unchanged
    from the previous turn; every other turn (turn 1, and a periodic refresh every
    `full_render_every` turns) gets a full render. Why: the model addresses elements purely by
    the `[index]` position in that turn's rendered list, so a diff sent while the element list
    itself changed would leave the model acting on a stale index -- a wrong-element click, not a
    token saving. `digest()` already deliberately excludes `value` (Phase 6), so an unchanged
    digest is the one condition under which every index is provably still correct. See
    `docs/adr/0010-diff-observations-and-stopping-conditions.md`.
58. Seven stopping conditions replace a single step cap: `goal_verified`, `max_steps`, `timeout`,
    `no_progress`, `loop_detected`, `dead_end`, `escalation`. Why: a step cap alone cannot say HOW
    a run is stuck, and the fix differs by shape. `no_progress` (actions dispatch, the page does
    not move) and `dead_end` (actions do not dispatch at all, because the target will not
    resolve) are deliberately different signals with different fixes -- a better prompt or
    checkpoint for the first, a better locator for the second. One shared `stall_limit` governs
    all three stall-style conditions (`no_progress`, `loop_detected`, `dead_end`), because they
    are the same "how many times before we call it" question, not three independently-tuned
    knobs. See `docs/adr/0010`.
59. `EscalationRequired` is now caught inside the discovery loop and ends the run with the
    `escalation` status, rather than propagating as an exception out of `run()` (Phase 5 left
    this open deliberately). Why: Phase 10's live handoff needs a run that ends with a status the
    caller can read, not a stack trace the CLI has to catch specially. `NavigationBlocked` still
    propagates uncaught: a session that left the allowlist is not a state worth resuming
    reasoning from at all.
60. Every tool call's `rationale` is validated once, immediately after the model's tool call is
    received and before any tool-specific branching -- including `finish` and `escalate`, which
    previously were not checked the same uniform way action tools were. Why: "every action tool
    requires a rationale" (Phase 2 decision 28) is truer as one gate all eight tools pass through
    than as a check duplicated, or forgotten, per tool.
61. `discover` never overwrites an existing `{slug}.v<N>.json`; it writes `{slug}.v<N+1>.json` and
    sets `capability.version` to match, so artifacts are append-only. No test may depend on the
    frozen content of a file under `artifacts/`, only on inputs it constructs itself. Why: a
    second real run of the same goal text silently destroyed this project's own non-negotiable
    Phase 2 artifact, which turned out not to be recoverable at all. See
    `docs/adr/0011-artifacts-are-versioned-and-tests-never-pin-to-them.md`.

## Phase 8 decisions

62. `Capability` carries every field the brief grades: typed `InputParam`/`OutputField` lists, a
    `Step.value` that is either a literal or a `ParamRef` naming a declared input, `known_outcomes`
    and `recovery_rules` seeded from a starter library, and a `stability` signal that stays a
    read-only observation, never a gate. `schema_version` (this file's own shape) and `version`
    (this recording's own revision) are deliberately two different counters answering two
    different reviewer questions. See `docs/adr/0012`.
63. Postconditions are derived from `policy_decision.checked_urls` progression, in precedence
    order (URL change first, then an extract step's own value, then the next step's target as a
    reachability check, with the last step always taking the run's own success checkpoint), because
    a true observation-diff needs a per-turn snapshot the evidence format does not yet carry. Why:
    every one of a real recording's steps needs a verifiable postcondition for replay to check
    against, not just the final one. See `docs/adr/0012`.
64. `Observation.urls` (every loaded frame) exists so `url_matches` checks frame identity, not a
    frameset's constant shell URL. Why: measured on this fixture, the shell stays `/app` on every
    screen, so a page-level check would silently pass on the wrong one. See `docs/adr/0012`.
65. Every schema field is marked STRUCTURAL or VALUE_CARRYING (`models/observation.py`), and
    `Redactor` walks the live model tree to apply R3 (the whole-string credential-shaped-literal
    rule) only to a VALUE_CARRYING field, never a STRUCTURAL one. Why: R3 alone destroyed a
    checkpoint value `DONE_TOKEN` and a URL path `/secret-flow`, both real regressions this
    measurably fixes; R1 and R2 still apply everywhere regardless of marking. See `docs/adr/0012`.
66. `record/recorder.py`'s dead-end pruning groups CONSECUTIVE same-state events into one run
    before pruning across runs, not raw events. Why: measured directly against the real recording,
    a per-event version of this rule mistook ordinary sequential form-filling (typing a username,
    then a password, both while the URL is still the login page) for a detour, and silently
    dropped 3 of 7 real steps. See `docs/adr/0012`.
67. `replay/engine.py` resolves a step's `ParamRef` and a checkpoint's `:name` placeholder through
    one shared function, validates every required `InputParam` is present before any browser
    launches, and registers a sensitive param's caller-supplied value with the run's `Redactor`
    before the first event is even logged. Why: Phase 8 introduced `ParamRef` into the schema
    without the executor to match, so a live replay typed the literal placeholder text into the
    form and compared a checkpoint against a route template no real page can ever match --
    shipping a schema feature and deferring its executor is not an option when doing so breaks the
    phase's own deliverable. Measured live: replaying with a different member than the one
    recording still fails, and earlier than expected -- not at the (also member-specific) success
    checkpoint, but at a locator step whose recorded accessible name embeds the member id too, a
    third place a single recording's literal leaks in that this phase's canonicalization does not
    yet reach. See `docs/adr/0013`.
68. A code-review pass on item 67 found four more correctness holes no test's synthetic data had
    the right shape to expose: `record/recorder.py`'s dead-end pruning erased the action that
    escapes a detour, not the detour itself; a pii-sensitivity Type was logged as a hardcoded mask
    instead of a parameter reference the recorder could bind to; a pii-classified parameter still
    carried the raw observed value in `example`; and checkpoint placeholder interpolation was a
    sequential, prefix-unsafe `str.replace` chain rather than one regex pass. All four are fixed.
    None changed the shipped artifact (verified by rebuilding it in memory and diffing field by
    field against the file on disk). See `docs/adr/0013`.
