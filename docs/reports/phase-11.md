## Phase 11 Verification Report

Status: COMPLETE

Loop summary: four rounds, the fourth after the user caught an environment-dependent green (see its
own section below: the suite passed under my invocation and refused to collect under CI's). Round 1 built the two halves (the `insufficient_funds` fix, then the
MCP catalog) and passed every gate, but independent verification found four defects: the calling
agent lost the reason a human had been called for whenever an escalation expired, the demo
transcript hid the approval that made act 2 possible, the demo crashed on a second run against an
already-approved artifact, and the transcript corrupted floats inside tool results. Round 2 fixed
all four and re-ran the demo. Round 3 took the three cuts from `/ponytail-review` (a duplicated
"highest version wins" rule in the demo, a redundant side-dict in the server, a nested guard in the
fixture) and fixed a fifth defect I found in round 2's evidence: the output directory held six
replay runs from two different rounds while its transcript described only one, including one
orphan showing the pre-fix result. The demo now wipes its own previous output before it writes.

Delegation: the builder wrote the fixture rule, the detector, the recorder predicate, the MCP
server, both CLI commands, the demo script, the tests and both ADRs. The main session set the
design decisions it built to (publish highest-version-per-capability-id, business outcomes and
escalations are normal results while hard failures are errors, approval is an out-of-band human
command the server can never perform), verified every gate independently rather than reading the
builder's summary, wrote and ran its own MCP client against the real `artifacts/` directory, ran
the over-engineering review, and ran the live replay that proves the new detector is not inert.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] **1. The server starts and `list_capabilities` returns both capabilities, each with a valid
  input schema** — ran: my own MCP stdio client (not the demo) against
  `understudy catalog --transport stdio --artifacts-dir artifacts` — got:

      server: understudy-catalog 1.0.0
      tools/list -> ['get_member_savings_balance', 'open_member_subaccount', 'list_capabilities']
      list_capabilities -> 2 capabilities

  Both schemas printed in full below. Expected: two capabilities, three tools, every tool name
  legal for MCP (`^[a-zA-Z0-9_-]{1,64}$`).

- [x] **1b. No declared type disagrees with its own example** — ran: the same probe, checking each
  `examples` entry against its property's declared `type` — got:

      check get_member_savings_balance.member_id: type=integer example=12345 -> ok
      check open_member_subaccount.member_id:     type=integer example=12345 -> ok
      check open_member_subaccount.initial_deposit: type=integer example=250 -> ok
      FAILURES: none

  `initial_deposit` is clean: it declares `integer` and carries the integer `250`, not `"250"`.
  The same check runs in the suite as
  `test_published_capabilities_examples_match_their_declared_types`, over the real `artifacts/`
  directory, reusing the replay engine's own param type-checker rather than a second type rule.

- [x] **2. Invoking the balance lookup with valid params returns Success with outputs** — ran: the
  demo, act 2, `{"member_id": 12345, "password": "..."}` — got:

      {"kind": "success", "outputs": {"savings_balance": "$1,204.55"}, "steps_run": 7,
       "duration_ms": 2782.0000000065193}   is_error: false

  Cross-checked against the engine's own `result.json` in
  `evidence/catalog-invocation/replay-idafcf0f2426ea/`, which was written by the replay engine and
  not by the demo, and agrees exactly.

- [x] **3. Invoking it with member 99999 returns a BusinessOutcome, not an error** — ran: the demo,
  act 3 — got:

      {"kind": "business_outcome",
       "code": "member_not_found",
       "message": "No member found for the given id.",
       "observed": "No member matches that search.",
       "outputs": {}}                       is_error: false

  `message` is the capability's own declared meaning, readable off the artifact before it is ever
  invoked. `observed` is what the application itself said. `is_error` is false, which is the whole
  point: a business outcome is an answer, not a malfunction.

- [x] **4. Invoking a draft artifact is refused with a message naming the status** — ran: my probe
  against the real `artifacts/`, and independently the demo's act 4 — got:

      is_error=True
      "capability 'open_member_subaccount' is status 'draft' and cannot be invoked;
       a human must review and approve it"

  Nothing launches: `test_draft_capability_is_refused_without_calling_the_replay_engine` replaces
  the engine entry point with a function that fails the test if it is ever entered.

- [x] **5. The risky path neither executes silently nor fails opaquely** — ran: the demo, act 6,
  against the now-approved subaccount capability whose final step is `RISKY_IRREVERSIBLE` — got:

      {"kind": "hard_failure",
       "step_id": 9,
       "category": "escalation_unresolved",
       "expected": "step 9 (click) to be permitted by policy",
       "observed": "risk_replay: RISKY_IRREVERSIBLE refused in replay: requires capability status
                    'approved' and allow_risky=True (got capability_status='approved',
                    allow_risky=False): the risky_labels heuristic did not match element name
                    'Submit', but a currently loaded URL ('/member/12345/subaccount/new') matches
                    a mutating_routes pattern (checked 3 loaded URL(s))
                    -- escalated as intervention 'esc397a29c840', which expired with no operator
                    resolution",
       "evidence_refs": ["steps/009_before.png", "steps/009_after.png", "dom/009.html",
                         "a11y/009.json", "trace.zip"]}

  Verified separately that it did not execute: act 6's own accessibility snapshot
  (`replay-id213251719b7b/a11y/009.json`) still shows the unsubmitted form, the loaded URL is still
  `/member/12345/subaccount/new`, and no confirmation reference or "Subaccount Opened" text appears
  anywhere in that run's evidence. Verified separately that a human was genuinely asked: the
  intervention record `esc397a29c840.json` carries `reason_code: risky_action_requires_approval`,
  step 9, the full policy reason, a screenshot, and the control token moving
  `automation -> pending_handoff` and back on expiry.

- [x] **6. The `balance_check` detector exists and resolves, and the subaccount capability declares
  `insufficient_funds` because its flow earns it** — the strongest check in this phase, because a
  detector that resolves but never fires is the failure mode this project treats as a correctness
  bug. Ran a live replay with the risky step permitted, against a deposit the member cannot cover:

      $ understudy replay --artifact <subaccount v2, approved copy> \
          --params '{"member_id":12345,"password":"...","initial_deposit":5000}' \
          --allow-risky --no-escalate
      BUSINESS OUTCOME (not a failure):
        code: insufficient_funds
        message: The submitted deposit exceeds the available balance.
        observed: Insufficient funds: the initial deposit exceeds the available balance of $1,204.55.

  Exit code 0. The re-recorded `known_outcomes`, and what in the flow earned each:

      member_not_found     / member_lookup_no_match  step 3 types into "Member ID" and step 4
                                                     clicks "Search": the flow performs a record
                                                     lookup, so the app can answer "no such member"
      permission_denied    / permission_denied       the flow's recorded URLs include /member/...:
                                                     the flow opens a protected record, so the app
                                                     can refuse it
      validation_rejected  / validation_rejected     a type step is followed by a click: the flow
                                                     submits typed input, so the app can reject it
                                                     as malformed
      insufficient_funds   / balance_check           step 8 types into "Initial Deposit" and step 9
                                                     submits it: the flow spends value, so the app
                                                     can refuse it for funds

  The read-only balance lookup still earns only the first three, and still cannot earn the fourth,
  because it never types into a monetary field. That is asserted by the Phase 9 test I left
  untouched.

- [x] **7. The demonstration transcript is saved under `evidence/catalog-invocation/`, with an
  explicit evidence dir** — ran: `ls evidence/catalog-invocation/` — got `artifacts/`,
  `interventions/`, three `replay-*` directories and `transcript.jsonl`, all from one run. The demo
  passes `--evidence-dir evidence/catalog-invocation` and `--intervention-dir` beneath it, and works
  against a copy of `artifacts/` so it never mutates the repository's real artifacts. Confirmed the
  password appears nowhere in the tree: `grep -ril "demo-password-not-a-real-secret" evidence/
  artifacts/` returns nothing.

- [x] **8. `docs/reports/phase-11.md` contains a copy of this report** — this file.

- [x] Full suite, BOTH invocations — ran from a fresh shell with a freshly started fixture on :5055:

      $ .venv/Scripts/pytest.exe                  (the console script; what ci.yml runs)
      280 passed in 107.71s (0:01:47)

      $ .venv/Scripts/python.exe -m pytest
      280 passed in 108.96s (0:01:48)

  Zero skips in both (`addopts` includes `-rs`, so a skip would print its reason; none did). 272
  before this phase, 11 added, 1 replaced. See "an environment-dependent green" below for why both
  invocations are now reported: the first one was broken by this phase and is the one CI and the
  README use.

- [x] Lint and types — ran: `.venv/Scripts/python.exe -m ruff check .` — got `All checks passed!`;
  `.venv/Scripts/python.exe -m mypy src/` — got `Success: no issues found in 40 source files`.

### Human-review items  (the user confirms these)

- [ ] **The balance capability now ships approved.** Check:
  `artifacts/look-up-member-12345-and-read-their-current-savings-balance.v3.json`, field `status`.
  What you should see: `"approved"`. I made this call so a reviewer who points an MCP client at
  `understudy catalog` finds at least one capability they can actually invoke; it is read-only with
  no irreversible step. The subaccount capability stays `draft`, which is the honest state for a
  flow whose last step is irreversible and has had no sign-off. The status flip was done by running
  `understudy approve`, not by editing the file. If you disagree, re-run the demo after setting it
  back and everything still works, act 0 just shows the draft-to-approved transition instead of
  "already approved".

- [ ] **The two published contracts are readable by a human and by an agent.** Check:
  `.venv/Scripts/python.exe -m understudy.cli catalog --transport stdio` and call
  `list_capabilities`, or read act 1 of `evidence/catalog-invocation/transcript.jsonl`. What you
  should see: two capabilities with typed input schemas, declared outputs, status and version.
  Whether that is enough for a calling agent to use them without reading the code is your judgment,
  not mine.

- [ ] **The resumed escalation path through the catalog is not demonstrated.** Check: act 6 of the
  transcript ends in an expired intervention, because nobody played the operator. To see the other
  half, start `understudy operator --store-dir evidence/catalog-invocation/interventions`, raise
  act 6 again with a longer `--intervention-ttl`, and approve it in the console. Phase 10 already
  has real evidence of a human resolving an intervention and the run continuing on the same
  session, which is why I did not script a fake operator to produce a nicer-looking result here.

- [ ] Committing, pushing and the CI run, as always.

### Invariants

    $ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -q
    .....                                                                    [100%]
    5 passed

None skipped. Invariant 1 (no model in the replay path) is worth noting this phase: the catalog
imports the replay engine and never touches `understudy.llm`, so an agent invoking a capability
runs a path with no model in it at any point.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

- Three live browser replays through the catalog in one demo run: 7 steps to a Success (2.78s),
  6 recorded actions to a business outcome, 11 to the escalation at step 9 of 10.
- The `balance_check` detector fired on its first live attempt, returning the application's own
  wording verbatim.
- Locator ranks in the two published capabilities: the subaccount capability resolved all 10 of its
  targets at rank 1 (role plus accessible name, the strongest strategy). The balance capability
  records `recorded_rank: null` for all 7, because it was recorded before that measurement existed
  and it has never been re-recorded. That is an honest gap, not a zero.
- One live model call this phase, and not in any agent loop: re-recording the subaccount artifact
  made a single Gemini metadata call for the capability's name and description. No discovery run
  was performed. The re-recorded artifact has a byte-identical `transcript_hash` and identical steps
  and inputs to v1, which is how I confirmed it came from the real Phase 10 run rather than a new
  one.
- Deleted after the over-engineering review: 16 lines across three files.

### How the core piece works  (plain English)

Each file under `artifacts/` is one recorded capability, and the catalog turns each one into a tool
an AI agent can call over MCP. The tool's name is the capability's name, its description is the
capability's description, and its input schema is the artifact's own `json_schema()`, so the
contract the agent reads is the artifact itself rather than a second description of it that could
drift. Only the highest version of each capability is published, and the whole directory is re-read
on every request, so recording a new version or approving one takes effect without a restart. When
the agent calls a tool, the catalog runs the deterministic replay engine with no model anywhere in
the loop and hands back the typed result: a success with its declared outputs, or a business
outcome like "no such member" or "insufficient funds" returned as a normal result rather than an
error, because those are answers the caller needs and not malfunctions. Two things it will never do:
it never passes the flag that permits an irreversible step, and it has no code path that can mark a
capability approved. So a draft capability is refused with its status named, and an approved one
whose final step is irreversible stops at that step, raises an intervention a human can act on, and
tells the calling agent exactly which step stopped, why it was judged irreversible, and which
intervention is waiting.

### Decisions logged

- docs/adr/0016-insufficient-funds-reinstated.md — Phase 9 cut this outcome for want of a reachable
  flow and a detector; Phase 10 recorded the flow, so Phase 11 wrote the detector and made the app
  able to say no, rather than leave a capability that reports a hard failure for a legitimate
  business answer.
- docs/adr/0017-capabilities-as-mcp-tools.md — why MCP rather than a bespoke endpoint, why the
  low-level server (the input schemas are dynamic, straight off the artifact), why highest-version
  publication, why business outcomes and escalations are not errors on the wire, why the catalog can
  never arm a risky replay or approve anything, and why the escalation TTL is minutes rather than
  the fifteen a blocking tool call cannot justify.

### An environment-dependent green  (the seventh instance of one family)

Found by the user after I reported this phase COMPLETE, and it is the most instructive defect in the
phase, so it goes here rather than buried in the caveats.

Every test result I reported above was first measured with `.venv/Scripts/python.exe -m pytest`.
Run through the console script instead, which is what `.github/workflows/ci.yml` line 18 runs and
what the README will tell a reviewer to run, the suite did not fail a test. It refused to start:

    $ .venv/Scripts/pytest.exe -q
    tests\test_phase11.py:25: in <module>
        from fixtures.legacy_bank.app import app as fixture_app
    E   ModuleNotFoundError: No module named 'fixtures.legacy_bank'
    !!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!

Cause: `[tool.setuptools.packages.find]` sets `where = ["src"]`, so the editable install exposes
`src/` only. `fixtures/` is a test target, not an installed package, and resolves solely because
`python -m` puts the current directory on `sys.path`, which the console script does not do.
`tests/test_phase11.py` is the first module in this project to import the fixture app directly;
every earlier phase reaches it over HTTP against a live server, which is why the gap stayed hidden
for ten phases and opened the moment this one landed. CI was not silently under-testing before now,
because nothing imported `fixtures` before now. It would simply have gone red on the next push.

Fixed in configuration, not by changing how the suite is invoked: `pythonpath = ["."]` added to
`[tool.pytest.ini_options]`. That is pytest's own setting (pytest >= 7; `minversion` here is already
8.0), so both invocations resolve identically with no `sys.path` manipulation in `conftest.py`.

Why not the alternative of making the import consistent with earlier phases: driving the fixture
over HTTP would turn the two insufficient-funds branch tests into live-server tests, which SKIP
when :5055 is unreachable. CI never starts the fixture, so the money path this phase added would
have had zero coverage in the one environment that always runs. A test that skips where it matters
most is the same defect in a different costume. Adding `fixtures` to the installed packages was also
rejected: shipping the deliberately hostile test target inside the distribution would be wrong.

`ci.yml` is deliberately left alone. It runs the bare console script, which is now the stricter of
the two invocations and the same one a reviewer will use.

Two things I re-measured after the fix, rather than assuming they still held:

- Both invocations, from a fresh shell, fixture live: 280 passed each, zero skips (above).
- The fixture process I had been testing against was itself started before round 3's edit to
  `fixtures/legacy_bank/app.py`, so it was serving code one revision old. That edit was a pure
  refactor of the same condition and the demo never reaches that branch at all (act 6 stops before
  the POST), but the live `insufficient_funds` proof under gate item 6 does reach it, so I restarted
  the fixture and re-ran that replay. Identical result: `code: insufficient_funds`, same observed
  wording, exit 0.

Why this is the same family as the earlier six, all of which were stale processes: in each case a
mechanism reported success inside the box it was measured in, and the box differed from the one it
would meet in production. A stale server serving old code and a test runner resolving imports one
way in my shell and another way in CI are the same failure. The rule this phase adds to the others:
a green is only as good as the invocation that produced it, so report the invocation, and prefer
measuring the one a stranger will use.

### Caveats / not done

- **A reversal of a Phase 9 decision, stated plainly.** `tests/test_phase9.py`'s
  `test_risky_or_deposit_flow_still_never_earns_insufficient_funds` asserted the exact opposite of
  what this phase requires, so it was replaced with a test asserting the new rule and its detector
  resolves. This is a requirement change handed down in the phase prompt, not a check weakened to
  make code pass. The companion test, that a read-only lookup still never earns the outcome, was
  left untouched and still passes, which is what stops the new seed from being over-broad.
- **`replay_end` is not a uniform terminal event.** A replay that ends early on a business outcome
  or a hard failure returns from inside the step loop, so its `run.jsonl` ends on that event rather
  than on a `replay_end`. Every run still writes a `result.json`, so no run lacks a terminal record,
  but a consumer tailing the event stream has to know that three event types can be the last one.
  Found while verifying this phase, out of its scope, not fixed.
- **The balance capability's v2 artifact loads again.** Phase 9 recorded that it deliberately no
  longer loaded, because it named the `balance_check` detector that did not exist. That detector now
  exists, so it loads. It is not published (v3 is higher) and it still carries the integer-versus-
  `"12345"` type disagreement that v3 fixed. It was not edited: it is real evidence of a real defect.
- **The escalation in act 6 expired rather than being resolved**, because the demo runs unattended.
  See the human-review item above for how to see the other half.
- The operator console is not started by the demo. The transcript prints the intervention id; a
  human wanting to act on it starts the console themselves.
