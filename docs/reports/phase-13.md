## Phase 13 Verification Report

Status: COMPLETE

Loop summary: two builder tasks plus three follow-ups. Task A made the two fixes that change what
gets written into evidence, before any evidence was produced: human-typed values are now suppressed
at the source for secret fields, and every replay ends its event stream with a terminal marker. Task
B produced six new replay runs, reproduced the cross-tenant pair and the catalog transcript, and cut
evidence/ from 59 directories to 12. One follow-up closed an untested branch in the suppression path.
One repointed three tests that were pinned to an evidence directory this phase deletes. One fixed a
stale run id naming a directory that no longer exists.

Delegation: the builder wrote both fixes and their tests, produced every run, and did the renames,
moves and deletions. The main session decided fix-versus-state on all four flagged items, wrote the
independent scans (secrets, PII, screenshot masking), measured every DoD claim itself rather than
reading the builder's summary, and wrote evidence/README.md.

### The builder deleted a directory three tests depended on, and said so

Worth recording because it is the phase's one real incident. While clearing the ~50 leftover working
directories, the builder ran `rm -rf evidence/discovery-*` before cross-checking test dependencies,
which took `evidence/discovery-adccddf2b6e5` with it. Three tests in `tests/test_phase10.py` read
that directory and went red. The builder reported it rather than quietly repointing or skipping them.

I did not restore it. That directory was never on the keep list and is not one of this phase's eleven,
so restoring it from HEAD would only mean deleting it again at the end of the phase. The real problem
it exposed is that three tests were pinned to evidence the phase requires removing. They now read
`evidence/discovery-subaccount`, a kept genuine recording of the same goal, with no assertion touched.
Risk is classified at replay time by `PolicyGate` from the live policy, not read off the artifact, so
the recording's vintage does not change what those tests assert. The builder checked that claim
against the moved intervention record instead of taking it from me, and found the docstring's "recorded
before the mutating-routes fix" framing was no longer true for the substitute; it was reworded rather
than carried over as a false claim.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] **1. Eleven directories, and nothing else.** Before: 59 directories, 11MB. After: 12 entries
  plus README.md, 3.8MB.

      $ ls evidence/
      README.md  artifacts  catalog-invocation  cross-tenant  discovery  discovery-subaccount
      escalation  replay-business-outcome  replay-different-member  replay-hard-failure
      replay-recovered  replay-retried  replay-success

  All eight single-run directories hold `run.jsonl` and `result.json` at their own root. Three are
  containers by nature: `cross-tenant/` holds the two runs side by side plus the capability copy that
  was replayed, `catalog-invocation/` holds the MCP transcript plus the three replays it triggered,
  and `artifacts/` holds the two published capabilities and the overlay. `evidence/interventions/`
  is gone; the two records that matter were moved into the runs they belong to and the other six
  deleted.

- [x] **2. HUMAN-REVIEW: the discovery transcripts are genuine model runs.** First tool calls of each,
  printed from `transcript.jsonl`, for you to judge:

      BALANCE (evidence/discovery/)
       1. type({"index": 11, "text": "admin"})       reason: Type 'admin' into Username field.
       2. type({"text": "admin", "index": 15})       reason: Type 'admin' into Password field.
       3. click({"index": 18})                       reason: Click Login button to log into the application.
       4. type({"text": "12345", "index": 21})       reason: Enter Member ID 12345 into the Member ID search box.
       5. click({"index": 23})                       reason: Click Search button to look up member 12345.
       6. click({"index": 27})                       reason: Click on member link '12345 - Testuser Alpha' to view member details.
       7. extract({"index": 34, "output_name": "savings_balance"})
                                                     reason: Extract the current savings balance for member 12345.
       8. finish({"checkpoint": {...text_present, "$1,204.55"}})
                                                     reason: Successfully looked up member 12345 and extracted the savings balance of $1,204.55.

      SUBACCOUNT (evidence/discovery-subaccount/)
       1. type({"text": "admin", "index": 11})       reason: Logging in to access the banking system.
       2. type({"index": 15, "text": "password"})    reason: Logging in to access the banking system.
       3. click({"index": 18})                       reason: Submitting the login credentials.
       4. type({"text": "12345", "index": 21})       reason: Searching for member 12345.
       5. click({"index": 23})                       reason: Clicking Search to find member 12345.
       6. click({"index": 27})                       reason: Selecting member 12345 to view their account details.
       7. click({"index": 35})                       reason: Opening the form to create a new subaccount.
       8. type({"text": "Vacation Fund", "index": 29}) reason: Entering the nickname for the new savings subaccount.
       9. type({"text": "250", "index": 33})         reason: Entering the initial deposit amount for the new subaccount.
      10. click({"index": 36})                       reason: Submitting the form to create the new subaccount.

  The balance run is only eight calls long because that is all it took. Both address elements by
  observation index, which is what a model reading a rendered accessibility tree does and what a
  replay never does. The two runs word their reasons differently for the same login step, which is
  what two separate model runs look like.

- [x] **3. No API key in any tracked file.** CLAUDE.md forbids running git by any route, so I could
  not use `git ls-files` as the phase prompt suggests. I enumerated the tracked set by applying
  `.gitignore`'s own rules instead, which achieves the stated purpose of the instruction (excluding
  `.venv/`, whose certifi and cryptography constants otherwise bury a real hit). Scanning for key
  VALUES, not the words:

      $ .venv/Scripts/python.exe scratchpad/tracked_scan.py
      === API KEY VALUES ===
      keys: 0 hit(s) across 395 tracked files

  Patterns swept: `AIza` plus 35 chars, `sk-` and `sk-ant-` prefixed tokens, `AKIA`, GitHub `ghp_`
  style tokens, Slack `xox` tokens, PEM private key headers, bearer literals, and any
  `GEMINI_API_KEY=` assignment carrying a value.

- [x] **4. Credential-pattern sweep over the tracked set: nothing.** Same 395 files, 0 hits. The one
  expected class of hit the prompt names did not appear, because I scanned for key SHAPES rather than
  the strings `AIza` and `sk-`: the phase reports that describe what earlier sweeps looked for
  contain those words without a following key body, so they match no pattern here.

- [x] **5. The hunter2 decision is made and visible.** Taken the whole way: the capture is fixed AND
  the record was regenerated, so there is no longer a plaintext password in the repository to
  disclose. This item was closed after my first report, by the user re-running the handoff by hand
  against the fix.

  Fixed: `WebSurface`'s injected listener writes `[SUPPRESSED]` in place of the value whenever the
  element is an `input type="password"`, so the value never leaves the page. `drain_human_actions`
  applies the project's own `classify_field_sensitivity` as a second layer, for a sensitive field name
  that is not a password input. The action is still recorded; only the value is gone. Two live tests
  cover the two layers, each verified to fail with its own line removed and pass with it restored.

  Regenerated, and verified by me against what is on disk rather than taken on report:

      $ read evidence/escalation/escc59ee3e456.json
      id               escc59ee3e456    run_id id27ad8528ca6a    reason session_expired
      resolution       took_control by operator
      human_actions    19
         'f1'  value=''  'a'  'ad'  'adm'  'admi'  'admin' x2      <- username, real keystrokes
         'f2'  value='[SUPPRESSED]'  x8                            <- password, every entry
      transitions      automation -> pending_handoff (runner)
                       pending_handoff -> human (operator)
                       human -> pending_resume (operator)
                       pending_resume -> automation (runner)
      screenshot       steps/001_escalation.png

  The username entries beside the suppressed ones still carry the operator's real keystrokes, which is
  what makes this suppression of one field rather than a blanket wipe. `evidence/README.md` now
  documents the mechanism and why suppression has to happen at capture (redacting the final value
  would leave every prefix, which reconstructs the password), instead of disclosing a gap.

  The old record `escb4e73c2bb2.json` is gone from `evidence/`, and the run it belonged to was
  replaced by the new handoff. The regenerated run is strictly better evidence: 7 steps, success, and
  its log carries `escalation_raised`, both `control_transition` events, `handoff_resumed`, and a
  `step_skipped_after_handoff` event where the resume logic found the human had already done that
  step's work and skipped it rather than repeating it.

- [x] **6. No realistic PII in any log or screenshot.** How it was checked: the same tracked-set scan,
  with the four PII shapes `safety/redact.py` itself sweeps for (SSN, card number, email, phone).

      === REALISTIC PII ===
        [docs]  ssn: 3 hit(s)     docs/reports/phase-1.md:177, phase-5.md:32, phase-5.md:37
        [other] ssn: 1 hit(s)     CLAUDE.md:201
        [tests] ssn: 13 hit(s)    tests/test_constraints.py:290, ... and 11 more
      pii: 17 hit(s) across 395 tracked files

  Every one of the 17 is the literal `123-45-6789`, which is invariant 3's own sentinel: CLAUDE.md
  defines it, the phase reports describe sweeping for it, and the tests assert it never survives
  serialization. Zero hits anywhere under `evidence/`. The fixture's seed data is five synthetic
  members with no addresses, no SSNs and no card numbers, which is why there is nothing else to find.

  One correction to my own first pass: the card-number pattern initially reported 80-odd hits inside
  `evidence/`, all of them the fractional digits of floats such as `0.0833740234375` in accessibility
  snapshots. Requiring the match not to sit inside a decimal literal removed all of them. I checked
  that rather than reporting either the false positives or a clean result I had not earned.

- [x] **7. Every screenshot that should have been masked was.** Measured, not asserted. I asked the
  live fixture where the password box actually is, then sampled that rectangle in every committed
  screenshot that shows the login screen:

      password field bounds from the live app: {'x': 579.84, 'y': 78, 'width': 177, 'height': 21}
      login-screen screenshots checked: 68
      unmasked password boxes:          0
      control: the username box was NOT solid black in any of them

  The control matters: if the username box were also black the "mask" would just be a black image and
  would prove nothing. Two corrections along the way, both mine: the first version sampled post-login
  screens where those coordinates hold ordinary content, and the second used a 2px inset that landed
  on the input's border, because the mask is drawn over the perception's bounds (x=583..753) which sit
  a few pixels inside the DOM box (x=579.8..756.8). Sampling the central 60% fixed it.

- [x] **8. The committed evidence shows current behaviour.** Every `type` action into the secret field,
  across all 14 committed runs, serializes as a parameter reference with a readable rationale beside
  it:

      replay-success            text=${param:password}  rationale="Type '[REDACTED]' into Password field."
      discovery-subaccount      text=${param:password}  rationale="Logging in to access the banking system."
      cross-tenant/tenant-b     text=${param:password}  rationale="Logging in to access the banking system."
      ... 12 more, same shape

  And the pre-Phase-5 shape, both text and rationale blanked, appears nowhere:

      $ (scan for proposed_action.text == "[REDACTED]" AND rationale == "[REDACTED]")
      none: no committed run shows the pre-Phase-5 blanked shape

  Where a rationale reads `Type '[REDACTED]' into Password field.`, that is the model having quoted
  the password inside its own reasoning and R3 masking the quoted literal. The sentence around it is
  intact, which is the distinction DoD 8 draws.

- [x] **9. No test-named or stray directories.** All 47 deleted directories were working output from
  earlier phases. Nothing named after a test survived, and no directory from a manual CLI run that
  omitted `--evidence-dir` remains. I re-checked that no test still names an evidence path that does
  not exist; the only apparent hits were tests building their own `tmp_path / "interventions"`.

- [x] **10. evidence/README.md** indexes all eleven, names `discovery-subaccount/` and `escalation/`
  as the two to read first and says why, and states what CI does and does not prove. Its
  password-handling section documents the suppression mechanism and why suppression has to happen at
  capture; it was a disclosure of the plaintext record until that record was regenerated, at which
  point a disclosure would have described a limit that no longer exists.

- [x] **11. Size.** 3.8MB across 12 entries, down from 11MB across 59 directories, of which
  2.6MB across 262 files is what gets committed. The 50MB cap was
  never the constraint. Playwright traces are excluded by `.gitignore`.

- [x] **12. Full suite under the invocation CI uses.**

      $ .venv/Scripts/pytest.exe
      328 passed in 133.95s (0:02:13)

      $ .venv/Scripts/pytest.exe tests/test_constraints.py -v
      5 passed in 0.41s

  Zero skips, with the fixture live. `ruff check .` clean, `mypy src/` clean across 41 files.

- [x] **13.** This file.

### Invariants

    $ .venv/Scripts/pytest.exe tests/test_constraints.py -v
    tests\test_constraints.py .....                                          [100%]
    5 passed in 0.41s

None skipped. Invariant 3 is the one this phase leaned on hardest: it is why the sentinel
`123-45-6789` appears in the tests at all, and why those 13 hits are the expected result of the PII
sweep rather than a finding.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

| directory | outcome | steps | wall | recoveries | drift |
|---|---|---|---|---|---|
| discovery | goal_verified | 8 | 98.0s | 0 | 0 |
| discovery-subaccount | goal_verified | 11 | 51.6s | 0 | 0 |
| replay-success | success, `$1,204.55` | 7 | 2.2s | 0 | 0 |
| replay-different-member | success, `$532.10` | 7 | 2.1s | 0 | 0 |
| replay-business-outcome | business_outcome, `member_not_found` | - | 1.8s | 0 | 0 |
| replay-recovered | success | 7 | 2.4s | 3 | 0 |
| replay-retried | success | 7 | 3.2s | 2 | 0 |
| replay-hard-failure | hard_failure, `app_error` | - | 1.4s | 0 | 0 |
| escalation | success | 7 | 43.5s | 1 | 0 |
| cross-tenant/tenant-a | success | 10 | 2.9s | 0 | 0 |
| cross-tenant/tenant-b | success | 11 | 3.0s | 0 | 2 |
| catalog-invocation (3 replays) | success, business_outcome, hard_failure | 7, -, - | 2.3s, 1.9s, 33.0s | 0 | 0 |

Every outcome matched what I predicted before the runs were made. The retry backoff came out at 250ms
then 500ms, increasing as designed. The dialog case dismissed six native dialogs across three steps.
The escalation run's 43.5s is mostly a human typing. The catalog's 33.0s hard failure is a real
30-second intervention TTL expiring with no operator.

### How the core piece works  (plain English)

The evidence directory is the part of this submission that cannot be argued with, so it is organized
for someone with five minutes rather than for disk space. Two directories carry the load. One holds a
real LLM run that drove a legacy UI to open an account, stopped at the step the policy gate judged
irreversible, waited for a human to approve it in the operator console, resumed on the same browser
session, and produced the capability artifact that ships in this repository. The other holds a replay
whose session expired halfway through, handed the browser to a human who logged back in by hand, and
carried on to a correct answer, with 19 of that human's actions and the whole custody chain recorded.
The remaining nine cover the cases a caller has to be able to tell apart: the same recording returning
different answers for different inputs, a legitimate business outcome that is not a failure, a
recovered dialog, a retried transient error, a hard failure with a debuggable error, one recording
running against two different tenants, and an AI agent calling the whole thing over MCP.

### Decisions logged

No new ADR. This phase produced and curated evidence and made two fixes to what gets written into it;
the reasoning for both fixes lives in the code comments at `_HUMAN_ACTION_SUPPRESSED` and
`_finish_replay`, and the disclosure lives in `evidence/README.md` where a reviewer will actually meet
it.

### Caveats / not done

- **The escalation record was regenerated by hand after this report was first written**, which closed
  the one caveat it originally carried. The old record's plaintext password is gone from the
  repository, not masked in place. Item 5 above has the verified details. The wall clock in the
  run-and-report table below is the new run's (43.5s, was 63.5s); nothing else in the table changed.
- **Two limits are stated rather than fixed, as instructed**, and both belong in REPORT.md section 4.
  The business outcome detectors match the application's own wording, so tenant B is detected only
  because it happens to render one of the strings `member_lookup_no_match` already scans for; a tenant
  wording it differently would not be detected, and the overlay has no field for detector vocabulary.
  The `app_fingerprint` hash covers frame count, a role:count map and the title text, so it always
  differs across tenants and carries no information in the cross-tenant case; it is a within-tenant
  version signal answering "did this tenant's app change since we recorded it", and the fix would be
  the overlay carrying its own expected fingerprint per tenant.
- **`docs/adr/` and `docs/reports/` still cite the old run-id directory names.** Those are dated
  point-in-time records and rewriting them to match a later rename would be revising history, so
  `evidence/README.md` says the names changed and that run ids inside the files are what identify a
  run.
- **`evidence/discovery-adccddf2b6e5` is gone from the working tree** and, unless you restore it, will
  be recorded as deleted in your next commit. It is recoverable from HEAD if you disagree with cutting
  it.
