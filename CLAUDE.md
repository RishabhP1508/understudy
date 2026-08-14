# Understudy — Claude Code Operating Instructions

## What this project is
Understudy gives an AI agent hands inside legacy applications that expose no API. An LLM drives a
real UI to accomplish a natural-language goal, the successful run is recorded as a typed versioned
capability artifact, and that artifact replays deterministically with no model in the decision loop.
When the system cannot safely proceed, it escalates to a human who takes control of the same live
session and hands it back.

The name is the design. An understudy learns a part by watching once, then performs it exactly, and
never improvises. That is discovery and replay in one word, and the second half is the load-bearing
half: replay has no model deciding anything. Use the name in README.md's opening line.

This is a take-home submission judged by engineers reading many submissions side by side. The graded
axes, in their stated order of weight: system design, correctness of the core loop, robustness and
error handling, human-in-the-loop escalation, generalization to heterogeneous and multi-tenant
environments, safety and data handling, code quality, communication. Effort follows that order.

The through-line, repeated because everything serves it: the model discovers, the artifact becomes a
reusable capability, deterministic replay is how the AI agent invokes it in production.

## This file is the complete requirements source
The original assignment brief is NOT in this repository and will not be provided. Do not ask for it,
do not look for it, and do not hedge on the grounds that you have not seen it. This file plus the
phase prompt you are given are the complete and authoritative statement of what is required. Where a
phase prompt says "the brief says X", treat X as a given requirement, not as a claim to verify.

If a phase prompt appears to conflict with this file, this file wins, and say so rather than guessing.
Where either says "the brief", it means the requirements register below, which is the brief's content
restated in full. Nothing you need is missing.

## Requirements register (the full target, all of it)
Every one of these must exist in the final submission. A thin but real version of each beats a
polished subset of some. This register exists so no requirement depends on an earlier phase prompt
still being in your context.

R1 GOAL-DRIVEN AGENT LOOP
  - Accept a goal AND a target (app, URL, or entry point) as input. Both are inputs; the target is
    not merely config.
  - LLM-driven observe, decide, act loop against a live surface until the goal is met or a stopping
    condition is hit: max steps, timeout, dead-end.
  - The agent actually interacts with a real UI: click, type, navigate, read state. Bias toward a
    mechanism that still works when the surface has no clean DOM.

R2 STRUCTURED ARTIFACT (an agent-invocable capability, not a step list)
  - ordered steps and actions
  - how each target element is identified, with the robustness reasoning recorded
  - typed input parameters the agent supplies per invocation
  - typed outputs and their shape
  - a checkpoint or success condition
  - versioned and reviewable by both a human and a calling agent
  - decoupled from the raw model transcript

R3 DETERMINISTIC REPLAY
  - Given an artifact and input parameters, replay with NO LLM in the decision loop.
  - Stable element targeting, verify the checkpoint, return declared outputs to the caller.
  - Detect and respond deliberately to runtime conditions rather than blindly proceeding: validation
    errors, record not found, permission denials, unexpected confirmation dialogs (including native
    browser dialogs), session and timeout expiry, transient slowness, slow or failed loads, outright
    app errors.
  - The result contract distinguishes three classes: expected business outcomes the caller needs to
    know about, recoverable conditions, and hard failures that stop with a debuggable error.
  - Report what step, what was expected, what was observed.

R4 SAFETY AND POLICY GUARDRAILS
  - Explicit configurable allowlist of permitted domains and routes and allowed action types. The
    agent must not act outside it.
  - Distinguish safe and reversible actions from risky and irreversible ones, and handle the risky
    class conservatively.
  - Never persist secrets or raw sensitive data (credentials, tokens, full PII) into artifacts or
    logs. Redact.

R5 EVIDENCE AND OBSERVABILITY
  - A structured log of what the agent did AND WHY. The "why" is a stated requirement, not optional
    colour; every action event carries a rationale field.
  - At least one richer signal on failure: screenshot, DOM snapshot, or trace.

R6 HUMAN-IN-THE-LOOP ESCALATION AND HANDOFF
  - Detect a stuck or blocked state and raise an intervention request carrying enough context to act
    on: which capability or goal, the current step, the current state or screenshot, and why it
    stopped.
  - The human operates THE SAME live session, not a fresh one, performs manual steps, and hands
    control back so the run resumes or completes.
  - Preserve context and evidence across the handoff, and record what the human did.
  - Automation must pause, cede control, and resume on the same session, and there must be a way to
    know who is or should be in control.

R7 DESIGN FOR HETEROGENEITY AND SCALE (design, not build)
  - Surface abstraction: how the artifact schema and replay engine extend from a modern web app to a
    legacy web app and to a desktop app, and what the seam is between perceiving or acting on a
    surface and the recorded flow.
  - Multi-tenant reuse: how an artifact is reused, or safely specialized, across tenants running the
    same vendor product rather than re-recorded per tenant, and how per-tenant and per-version drift
    is detected and managed. Automation should generalize or DEGRADE GRACEFULLY across tenants.
  - Multi-tenant and desktop support need not be implemented. The core abstractions must not paint
    the design into a corner.

R8 THE ONE NON-NEGOTIABLE
  - At least one genuine LLM-driven discovery run against a live surface, with the evidence in
    /evidence/ proving it happened. A description of a run is worth nothing here.

## Deliverables contract (exact paths, exact headings)
Submissions are read side by side, so a wrong path or a misspelled heading costs marks for free.

1. `/README.md` covering how to set up and run it, including any keys or config needed AND how to run
   without live services, plus a demo path giving the exact commands to run the agent on a goal and
   then replay the resulting artifact.

2. `/REPORT.md`, one to three pages, using exactly these seven headings, in this order, spelled this
   way:
     1. Architecture
     2. Artifact schema
     3. Determinism & error handling
     4. Heterogeneity & multi-tenant
     5. Escalation & handoff
     6. Safety
     7. Cuts

3. `/evidence/` containing a saved example artifact plus logs from both a discovery run and a replay
   run, including at least one replay that hits an error or exceptional state.

Every choice below was ours to make and every one must be defended in REPORT.md: language, runtime
and frameworks; LLM provider and model AND how the agent loop is prompted and structured; the
computer-use technology; the target application; the artifact schema AND how it is stored and
serialized; how determinism is achieved on replay including locator strategy, fallbacks, and waiting;
and architecture and boundaries including single process versus services and synchronous versus
queued.

## The two-model build loop (follow exactly)
This session (the main session) runs on Opus 5 and is the ORCHESTRATOR and VERIFIER. Coding is
delegated to the `builder` subagent, which runs on Sonnet 5. You do not write feature code yourself;
you direct, verify, and decide.

For each phase:
1. WORK ONE PHASE AT A TIME. Build only the current phase's scope. No gold-plating.
2. Before delegating, re-read this file and ARCHITECTURE.md.
3. Delegate the implementation to the `builder` subagent with a clear, specific scope for this phase.
4. Classify every Definition-of-Done item as MACHINE-CHECKABLE (verifiable by a command whose output
   you can read: exit code, test result, grep, file contents, HTTP status, a JSON field, a pixel
   comparison) or HUMAN-REVIEW (no possible programmatic check; state exactly why).
   Prefer machine-checkable wherever technically possible.
5. VERIFY INDEPENDENTLY. Do not trust the builder's summary. Run every machine-checkable check
   yourself, read the actual diff, and confirm each Definition-of-Done item is genuinely met, not
   superficially. Actively check that the builder did not game any check (see anti-gaming rules).
   Run `/ponytail-review` on the diff as part of this step, except in Phase 8. It hands back a
   delete-list, and anything on it that is not covered by the "specified is not speculative" carve-out
   below should go back to the builder.
6. THE LOOP. If all machine checks pass and the code is sound, go to step 7. If not, send the builder
   back with the specific, real cause and the fix needed, then re-verify. Loop only while making net
   progress (fewer failing checks than the previous round). Hard-stop after 5 rounds OR on a round
   with no net progress, and report the phase BLOCKED with which checks still fail, the real reason,
   and what was tried. Never present a phase with a failing machine gate unless it is BLOCKED.
7. Produce the Phase Verification Report (format below) and STOP. Wait for the user to verify and say
   "proceed to Phase N".
8. Log non-trivial decisions as short ADRs in docs/adr/ (context, decision, tradeoff, alternatives).
   These are the raw material for REPORT.md in Phase 14, so write them as you go rather than
   reconstructing them at the end.

### Why the split matters
The builder has a stake in its own checks passing; you do not. Verify as an independent reviewer, not
as the builder's editor. This separation is the main guard against gamed checks, so use it: re-run the
checks yourself and re-derive whether each goal is truly met.

### Anti-gaming rules (non-negotiable)
Success criteria are FIXED and EXTERNAL. The implementation may change to pass them; the criteria may
never change to pass. Neither you nor the builder may: weaken, delete, skip, or xfail a test, or make
an assertion trivial; hardcode an expected answer or value; lower a threshold, loosen a tolerance, or
rewrite a check to be easier; catch and swallow errors to fake a success exit code; or stub or mock
the thing under test so it returns a canned pass. If the only way to pass a check is to alter the
check, the work is not done: report BLOCKED.

### The evidence carve-out (the most important rule in this project)
- FUNCTIONAL and SAFETY checks are HARD GATES: loop until they pass. Examples: test suite exit code
  0; a navigation off the allowlist is refused; a serialized artifact contains no sentinel secret; a
  replay against the not-found member returns a BusinessOutcome and not a failure; the policy gate
  refuses dispatch while the human holds the control token; no LLM import reachable from replay/.
- EVIDENCE IS PRODUCED, NEVER AUTHORED. Everything under evidence/ and artifacts/ must be the actual
  output of actually running the system. Never hand-write an artifact JSON, never author a run.jsonl,
  never fabricate or edit a model transcript, never stage a screenshot, never edit a result.json. The
  brief's one non-negotiable is that the discovery run is real, and a fabricated one is worse than a
  missing one, because it is the single thing the reviewer will check hardest and the easiest thing
  to detect.
- MODEL PERFORMANCE is RUN-AND-REPORT, never chased or gamed: how many steps a discovery run took,
  how many turns were rejected, replay stability across N runs, and the distribution of locator
  resolution ranks. Report the real numbers. If discovery struggles, the ONLY legitimate responses
  are (a) improve the observation rendering, the system prompt, or the tool schemas, or (b) report
  honestly for the user to judge. NEVER reduce the hostility of the fixture app to make the model's
  job easier: the hostile surface is the point of the project, and a sanitized one invalidates the
  locator strategy that the submission is mostly graded on.

## The five invariants (tests/test_constraints.py — NEVER EDIT)
These are written in Phase 0 before any feature code, because they are the constraints most likely to
erode over a long build and prose does not survive fifteen phases. They are the analog of a frozen
eval set: fixed, external, and not yours to adjust.

1. **No model in the replay path.** Nothing importable from src/understudy/replay/ may transitively import
   an LLM client or a provider SDK. Enforced by an AST import walk, not a grep.
2. **Every action passes the policy gate.** Surface.act is only ever called from PolicyGate.dispatch.
   No other call site exists anywhere in src/. Enforced by an AST scan of call sites.
3. **Nothing sensitive is serialized.** Serializing an artifact or a run log containing the sentinel
   values SECRET_SENTINEL_VALUE and 123-45-6789 produces output containing neither, checked in utf-8,
   base64, and url-encoded forms.
4. **The artifact is decoupled from the transcript.** A serialized capability has no key matching
   messages, transcript, completion, choices, or content, and provenance.transcript_hash is a hex
   digest.
5. **The model never self-declares success.** The finish tool schema requires a checkpoint field, and
   the loop's terminate path calls a deterministic verifier rather than returning on the tool call.

All five go live in Phase 2 against correctly-shaped stubs, so none sits skipped for long. A skip must
never be silent: it prints a reason naming the phase that will enable it.

## Architecture and design constraints (NON-NEGOTIABLE)
- TWO PATHS, ONE CONTRACT. Discovery has the model in the loop and runs once. Replay has no model and
  runs many times. The capability artifact is the only thing that crosses between them.
- THE SEAM IS THE SURFACE PROTOCOL. observe() and act(). The recorded flow refers only to normalized
  roles and accessible names, never to CSS, so the artifact is surface-agnostic by construction. This
  is the answer to the brief's heterogeneity question and it must be real, not asserted.
- PERCEPTION IS ACCESSIBILITY-FIRST. The brief says to bias toward what works with no clean DOM. The
  accessibility tree is also what Windows UI Automation exposes, which is what makes the desktop story
  credible. The model never sees raw HTML.
- ONE CHOKE POINT. PolicyGate.dispatch is the single path to any action, in both execution paths. The
  model only ever proposes actions; it never touches the browser. This is a stronger safety argument
  than a list of checks, and it is enforced by invariant 2.
- LOCATORS ARE RANKED DESCRIPTORS, NOT SELECTORS. role plus accessible name first, then scope, then
  relational hints, then ordinal, and a CSS fallback last and explicitly marked brittle. Resolution
  requires a UNIQUE match; a strategy matching two elements is skipped, never first-match-wins.
- BUSINESS OUTCOMES ARE NOT FAILURES. "No such member" is a legitimate answer the caller needs. The
  brief's glossary names conflating these as the most common design mistake in this problem. Detector
  evaluation order is: known business outcomes first, recovery rules second, checkpoint last.
- ONE SERIALIZATION PATH. Everything written to disk goes through Redactor. There is no unredacted
  write path, including screenshots, which are masked before the PNG bytes are written.
- NO SCALING INFRASTRUCTURE. No queues, no workers, no brokers, no clusters, no microservices, no
  Docker orchestration. The brief explicitly does not reward it. Single process, plus one local web
  process for the operator console. Simpler is fine when justified, and it is justified.
- THE FIXTURE APP IS A FIXTURE. fixtures/legacy_bank/ is a test target, not evaluated code. Keep it
  small, keep it hostile, do not polish it.
- PROSE IN A HUMAN VOICE. Write README.md, REPORT.md, and every human-facing doc using the writing
  skill at .claude/skills/human/SKILL.md: start with the point, be specific, no promotional words,
  restrained formatting, never use em dashes. Communication is an explicit grading axis, and a README
  that reads as AI-generated costs marks on a submission about judgment.
- NO VERSION CONTROL, BY ANY ROUTE. Never run git or the gh CLI, and never use a GitHub MCP or API
  tool to init, add, commit, push, tag, branch, create a repository, or open a pull request. The user
  does all of it manually. You only create and edit files on disk. Writing a file under
  .github/workflows/ is allowed and expected; committing it is not. (.claude/settings.json also hard-
  blocks both paths.)

## Conventions
- Python 3.11+, Pydantic v2 for every model that hits disk. No dataclasses for serialized types.
- Playwright Python library for browser control, launched HEADED. Headed matters for the escalation
  handoff in Phase 10, so do not switch to headless for convenience.
- typer for the CLI, FastAPI for the operator console.
- ruff to lint, mypy to type-check, pytest for tests. Type hints on every public function.
- All config via environment variables and policy YAML; never hardcode secrets; use a gitignored .env
  and document required vars in .env.example.
- No fixed sleeps anywhere in src/understudy/replay/. Explicit waits on conditions only.

## Repository layout (put every file exactly here)
Do NOT create new top-level folders or invent alternate file names. When a phase needs a new file,
place it in the folder shown and match the naming already established. Bracketed tags show the phase
in which each file first appears; a second tag means it is substantially rewritten then.

    understudy/
    ├── CLAUDE.md                          # these instructions (Part 2)                    [P0]
    ├── ARCHITECTURE.md                    # decisions and why                       [P0, grows]
    ├── README.md                          # setup, no-live-services path, demo commands   [P14]
    ├── REPORT.md                          # the seven-heading design write-up             [P14]
    ├── pyproject.toml                                                                      [P0]
    ├── .env.example                       # GEMINI_API_KEY placeholder                     [P0]
    ├── .gitignore                                                                          [P0]
    ├── .claude/
    │   ├── agents/builder.md              # Sonnet builder subagent (Part 1)
    │   ├── settings.json                  # git hard-block (Part 1B)
    │   └── skills/human/SKILL.md          # human-writing skill for README + REPORT      [you]
    ├── .github/workflows/ci.yml           # ruff, mypy, pytest; no live services, no key   [P0]
    ├── docs/
    │   ├── reports/phase-0.md ...         # a copy of each Phase Verification Report [P0..P14]
    │   └── adr/                           # short decision records                 [P2 onward]
    ├── policies/
    │   ├── legacy_bank.yaml               # allowlist, actions, risk labels, limits [P0 stub, P5]
    │   └── legacy_bank_tenant_b.yaml      # tenant B policy                              [P12]
    ├── overlays/tenant_b.json             # cross-tenant overlay                         [P12]
    ├── fixtures/legacy_bank/              # the hostile target app            [P1, +P12 tenant B]
    ├── src/understudy/
    │   ├── cli.py                         # discover | replay | operator | catalog        [P0]
    │   ├── config.py                                                                      [P0]
    │   ├── models/
    │   │   ├── observation.py             # UIElement, Observation                   [P2, +P3]
    │   │   ├── artifact.py                # Capability, Step, TargetDescriptor      [P2, +P8]
    │   │   ├── result.py                  # the result union                        [P2, +P6]
    │   │   └── intervention.py            # InterventionRequest, Resolution             [P10]
    │   ├── surface/
    │   │   ├── base.py                    # Surface protocol, Action union           [P2, +P3]
    │   │   ├── web.py                     # Playwright + a11y perception             [P2, +P3]
    │   │   ├── locator.py                 # TargetDescriptor, resolve, describe  [P2 stub, P4]
    │   │   └── desktop_stub.py            # documented UIA seam                          [P3]
    │   ├── llm/
    │   │   ├── base.py                    # LLMClient protocol                       [P2, +P7]
    │   │   └── gemini.py                  # Gemini function-calling implementation   [P2, +P7]
    │   ├── agent/
    │   │   ├── loop.py                    # observe -> decide -> act                 [P2, +P7]
    │   │   ├── tools.py                   # the tool schemas                         [P2, +P7]
    │   │   └── prompts.py                 # the system prompt                        [P2, +P7]
    │   ├── record/
    │   │   ├── recorder.py                # event log -> Capability                  [P2, +P8]
    │   │   └── canonicalize.py            # route + value parameterization                [P8]
    │   ├── replay/
    │   │   ├── engine.py                  # deterministic execution                  [P2, +P9]
    │   │   ├── outcomes.py                # business outcome detectors                    [P9]
    │   │   └── recovery.py                # recoverable condition handlers                [P9]
    │   ├── safety/
    │   │   ├── policy.py                  # PolicyGate, the choke point          [P2 stub, P5]
    │   │   ├── risk.py                    # reversible vs irreversible                    [P5]
    │   │   └── redact.py                  # the single serialization path        [P2 stub, P5]
    │   ├── evidence/logger.py             # run.jsonl, screenshots, snapshots    [P2 stub, P6]
    │   ├── escalation/
    │   │   ├── control.py                 # ControlToken, SessionBroker                  [P10]
    │   │   ├── store.py                   # file-backed intervention store               [P10]
    │   │   └── operator_app.py            # FastAPI operator console                     [P10]
    │   └── catalog/server.py              # capabilities as callable tools               [P11]
    ├── artifacts/                         # recorded capabilities        [P2 onward, generated]
    ├── evidence/                          # run output           [P2 onward, curated at P13]
    └── tests/
        ├── test_constraints.py            # THE FIVE INVARIANTS — NEVER EDIT              [P0]
        └── ...                            # per-phase tests

## Tooling (what to use, and when)
Plugins are switched on and off in .claude/settings.json, not here. This section says how to use the
ones that are on.

- **context7** — before writing code against a library whose API you are not certain of, pull current
  docs. Use it for the Playwright accessibility snapshot API, Playwright frame traversal, Pydantic v2
  discriminated unions, FastAPI, and the Gemini function-calling schema. Guessing an API from memory
  costs a loop round; checking costs one call.
- **playwright** — use to verify the fixture app renders and that a page you built is reachable.
  Never use it in place of writing Playwright library code in src/understudy/surface/web.py.
- **code-review** — use during independent verification, when reading the builder's diff. Phases 4,
  8, and 9 are where it earns its keep, since those are the pieces the reviewer will read hardest.
- **security-guidance** — use in Phase 5 when touching redaction, the allowlist, and risk
  classification, and again in Phase 13 before evidence is committed to a public repo.
- **frontend-design** — Phase 10 only, and keep the operator console deliberately plain. It is a mock
  by design and polish there is wasted effort.
- **ponytail** — always on, at `full`, and it applies to the builder too. Before writing code it walks a
  ladder: does this need to exist, is it already in the codebase, does stdlib do it, does the platform
  do it natively, is an installed dependency enough, is it one line, and only then the minimum that
  works. This is a mechanism for the thing the graded axes above actually reward, appropriate
  simplicity, and it is the main defense against a Sonnet builder gold-plating across fifteen phases.
  Leave `ponytail:` markers on deliberate shortcuts; `/ponytail-debt` harvests them into a ledger that
  becomes REPORT.md section 7.

  SPECIFIED IS NOT SPECULATIVE. Ponytail's first rung asks whether a thing needs to exist. For this
  project the answer is already fixed for three areas, and the ladder does not get to relitigate them:
  the artifact schema fields listed in Phase 8, the four-state control token and the eight reason
  codes in Phase 10, and the ranked strategy list in Phase 4. Those are not speculative abstraction;
  they are the deliverable the brief grades hardest. Cutting a schema field because it has no consumer
  yet is exactly the failure mode to avoid, since the consumer is a reviewer, not code. Everywhere
  else, apply the ladder hard: the fixture app, the CLI, the operator console, the evidence plumbing,
  and every helper the builder is tempted to invent.

  Run `/ponytail off` for Phase 8 only. That phase is deliberate richness by design and the ladder
  fights it. Run `/ponytail full` again at Phase 9.
- **andrej-karpathy-skills** — always on. It reinforces this file: no silent assumptions, surface
  inconsistencies instead of guessing, no overcomplication, surgical changes only. Where it and this
  file disagree, this file wins.

Do not enable, install, or invoke plugins set to false in .claude/settings.json, and do not add new
MCP servers or plugins without asking the user first.

## When CI goes red (from Phase 0 onward)
You have no GitHub access by design, so the loop is: the user pushes, and if the workflow fails, the
user pastes the failing step's log into the session. Treat that pasted log as the failure output,
apply the normal loop (diagnose the real cause, fix the implementation, never weaken the check), and
tell the user what to re-push. Never ask for GitHub credentials, never suggest enabling a GitHub MCP
or the gh CLI, and never offer to push the fix yourself.

Before the user pushes, reduce the chance of a red run: execute the exact commands the workflow runs,
locally, and report the result. CI here runs with no API key and no live services, so the most likely
red is a test that quietly depends on one. Catch that locally.

## Model routing
- Main session (this one): Opus 5. Orchestration, verification, the artifact schema, the locator
  resolution logic, the error taxonomy, the control-transfer model, ADRs, and hard debugging.
- builder subagent: Sonnet 5. All feature coding and boilerplate: the fixture app, Playwright
  plumbing, CLI wiring, FastAPI handlers, test scaffolding, CI YAML.

## Verification report format (produce this at the end of every phase)
Print the report in the session AND save an identical copy to docs/reports/phase-N.md. The saved copy
matters: the user commits it, and it is reviewed outside this session. Write it so it stands alone,
with real command output rather than summaries of output, and state honestly what is not done. Do not
pad it.

    ## Phase N Verification Report
    Status: COMPLETE | BLOCKED
    Loop summary: <rounds run; what the builder fixed each round>

    ### Machine-checkable gate  (ALL green for COMPLETE)
    - [x] <item> — ran: `<command>` — got: <actual output> — expected: <expected output>
    - [ ] <item> — BLOCKED: <the real reason, and what was tried>

    ### Human-review items  (the user confirms these)
    - [ ] <item> — check: `<command or file>` — what you should see: <observable result>

    ### Invariants
    $ pytest tests/test_constraints.py -q
    <actual output, including which are still skipped and why>

    ### Run-and-report numbers  (reported, NOT gated, never optimized against)
    <discovery steps taken, turns rejected, replay stability, locator rank distribution — whichever
    apply to this phase; "none this phase" is a valid answer>

    ### How the core piece works  (plain English)
    <one paragraph the user can re-explain in an interview>

    ### Decisions logged
    - docs/adr/000X-<slug>.md — <one line>

    ### Caveats / not done
    <anything honest to flag>

## What is on the user, not the tools
- ALL repository and GitHub work is the user's, by hand: creating the repo, staging, committing,
  pushing, branching, opening pull requests. Never do any of it, by CLI or by MCP. Any Definition-of-
  Done item that depends on a commit, a push, or a GitHub Actions run having happened is a
  HUMAN-REVIEW item, never a machine-checkable one.
- Reading the recorded artifact and judging whether a human reviewer could understand it is the
  user's call, not yours. Print it; do not grade it.
- Judging whether REPORT.md defends every decision is the user's call. They have to defend it in an
  interview, so they read it.
- Understanding the code well enough to defend any part of it is the user's own responsibility,
  outside this loop. The brief says explicitly that they own everything they submit.