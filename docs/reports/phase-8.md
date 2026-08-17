## Phase 8 Verification Report
Status: COMPLETE

Loop summary: 3 rounds. Round 1 built the full schema, the canonicalizer and the recorder rewrite,
and produced a v2 artifact that was rich, well-shaped, and completely unexecutable. Round 2 fixed
that: replay was discarding the caller's parameters entirely and typing the literal string
`${param:member_id}` into the form, and the canonicalized `:member_id` placeholders in postconditions
were never resolved back. Round 3 fixed four correctness holes the code-review plugin found that the
tests did not, the worst being a pruning pass that deleted the wrong side of a detour.

Delegation: builder wrote the schema, canonicalize.py, the recorder rewrite, the replay
interpolation, `tests/test_phase8.py`, and ADRs 0012 and 0013. Main session verified the phase's five
stated gaps against the real files before delegating, made the design rulings the brief left open
(postcondition derivation source, url_matches frame semantics, field marking, graceful degradation of
the recorder's model call), found the unexecutable-artifact regression by replaying it, ran the
code-review pass, and adjudicated a changed test expectation.

Ponytail was OFF for this phase, per the phase instruction, and restored to `full` at the end.

### The regression that mattered

The v2 artifact declared `member_id` and `password` as inputs and then ignored them:

```
step=1 type text='${param:password}'
step=3 type text='${param:member_id}'
url    -> http://127.0.0.1:5055/members?f7=%24%7Bparam%3Amember_id%7D
```

It typed the placeholder into the live form. R3 is "given an artifact AND input parameters, replay
with no LLM in the decision loop", and an executor that discards the parameters the artifact declares
is not honouring that. The same round found the mirror image: canonicalization rewrote postconditions
to `.../members?f7=:member_id`, which no real URL can ever match. Both are now fixed by one
interpolation path shared between step values and checkpoint values, so the two cannot disagree about
what `:member_id` means.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] **1. Invariant 4 non-trivially against a real recorded artifact** — `pytest
  tests/test_constraints.py -rs` → `5 passed`, no skips. The scan covers every file in `artifacts/`.

- [x] **2. At least one ParamRef and one canonicalized route** — inputs `['member_id', 'password']`,
  two ParamRef step values, and two canonicalized routes:
  `http://127.0.0.1:5055/members?f7=:member_id` and
  `http://127.0.0.1:5055/member/:member_id/balance`.

- [x] **3. Pruning verified** — a synthetic three-action dead end is removed. I re-derived the
  semantics myself rather than trusting the test:
  ```
  ['A','A','B','A','C'] -> ['A0', 'A3', 'C4']      detour A1->B2 dropped, sequential A0 kept
  ['A','B','C','A','D'] -> ['A3', 'D4']            detour A0->B1->C2 dropped
  ```

- [x] **4. Every step has a non-null postcondition** — all 7, and they are varied rather than filler:
  `element_present` for the three steps that do not change the URL, `url_matches` for the three that
  do, `text_present` for the extract.

- [x] **5. Both directions of the parameter contract** — every ParamRef resolves to a declared
  InputParam and every declared InputParam is referenced: `refs<=decl and decl<=refs: True`.

- [x] **6. Confidences serialize rounded** — `[0.5, 0.5, 0.9, 0.65, 0.95, 0.9, 0.65]`. The float noise
  (`0.49999999999999994`, `0.9500000000000001`) is gone. See the caveat about the literal grep.

- [x] **7. A terminal record for any run death** — `cli.py` now writes a terminal event and a
  `result.json` on any exception, including one raised inside `llm.complete()`, covered by a test with
  a raising stub. On `evidence/discovery-a3a4a2fc6000`: it CANNOT be explained from the directory
  alone. It holds a `run_start` and one `act` event and nothing else. I know externally that it died
  on a 404 from the retired `gemini-2.5-flash-lite`, but that knowledge is not in the run directory,
  and writing it in retrospectively would be authoring evidence. It stays as-is; Phase 13 deletes it.

- [x] **8. observation_digest** — it was null in all 76 occurrences across every log, and no caller
  ever passed it. It is now written on every act and decide event. The phase brief's inference that a
  stopping condition was therefore unreachable is WRONG and I am not repeating it: `agent/loop.py`
  computes `observation.digest()` in memory and compares it locally, so `no_progress` has always
  worked and `test_stop_no_progress` proves it. The real defect was narrower: the digest was not
  observable in the evidence, so a reviewer could not see why `no_progress` fired or reconstruct the
  page progression.

- [x] **9. Rationales non-empty and verbatim** — asserted by string equality against the source log,
  not eyeballed: `rationales verbatim: True`.

- [x] **10. `json_schema()` produces JSON Schema for the inputs** —
  `{"type": "object", "properties": {"password": {"type": "string", ...}, "member_id": {"type":
  "integer", "examples": ["12345"], ...}}}`. It is an instance method, correctly, since the input
  schema is per-capability.

- [x] **11. Round-trips losslessly** — `json.loads(cap.model_dump_json()) == artifact: True`,
  including a populated StabilitySignal in the test.

- [x] **12. Password records `sensitivity=secret` and its value is absent** — confirmed on disk, and
  live: replaying with a sentinel password gives `0` occurrences of it in run.jsonl, logged as
  `${param:password}` with the rationale reading `Type '[REDACTED]' into Password field.`

- [x] **13. Two version fields explained** — in the `models/artifact.py` module docstring, which is
  the right home since the explanation spans three fields across two classes. It distinguishes
  `schema_version` (the evolution of the schema shape), `version` (the revision of this recording),
  and `provenance.perception_version` (a third drift class), the last against the measured Phase 3
  failure rather than a hypothetical.

- [x] **14. `target.entry_point`** — `http://127.0.0.1:5055/login`, the resolved `--target`.

- [x] **16. ADRs at the next free numbers** — `0012` (schema richness, recorder as a pass, field
  marking) and `0013` (replay interpolation and the single-recording limit).

- [x] **17. This report saved** — `docs/reports/phase-8.md`.

Supporting gates and live runs:
```
$ pytest -q                        -> 180 passed
$ ruff check . / mypy src/         -> clean
$ grep -rn "\.act(" src/           -> one call site
replay v2 member_id=12345          -> success, savings_balance "$1,204.55", 7 steps
replay v2 member_id=12345 (int)    -> success  (matters: the schema declares integer, Phase 11 calls these as tools)
replay v1                          -> success  (regression baseline intact)
missing required param             -> hard_failure invalid_params, before any browser launches
```

### The subaccount capability, and why its absence is correct

It is not recordable this phase, and that is the designed outcome rather than a gap. The post-fix
subaccount run ends in `escalation` at the submit, because the fixed policy gate correctly classifies
that click as RISKY_IRREVERSIBLE. The recorder only runs on a verified success, so there is nothing to
record. Reaching the confirmation screen requires a human to approve an irreversible action on the
live session, which is Phase 10. I did not lower the policy, did not pick an easier goal, and did not
record from the pre-fix log. The pre-fix artifact remains quarantined at
`docs/quarantined-artifact-prefix-policy-defect.json` and was not read as input.

### Human-review items  (the user confirms these)

- [ ] **15. Read the artifact and decide whether a reviewer could say what it does, what it needs, and
  what it returns** — check: `artifacts/look-up-member-12345-and-read-their-current-savings-balance.v2.json`
  (313 lines, printed in the session). What I would point at: `description` is a sentence a calling
  agent can act on, `inputs` declares two typed parameters with one marked secret, `outputs` names one
  field with the step that produced it, every step carries the model's own verbatim rationale and a
  postcondition, and `known_outcomes` and `recovery_rules` are seeded. Whether that is enough is your
  call, not mine.
- [ ] The two ADRs defend the decisions, particularly the postcondition derivation and field marking.
- [ ] CI green after push.

### Invariants

```
$ .venv/Scripts/python.exe -m pytest tests/test_constraints.py -rs
.....                                                                    [100%]
5 passed in 0.28s
```

### Run-and-report numbers  (reported, NOT gated)

- Recorded from `evidence/discovery-b2405e162ba4`, 8 act events in, 7 steps out. Nothing pruned: this
  run had no detour.
- Resolution confidences as recorded: `[0.5, 0.5, 0.9, 0.65, 0.95, 0.9, 0.65]`. The two 0.5 values are
  the login fields, whose names are derived from table structure rather than authored by the app.
- Replay stability across the runs I did this phase: 4 of 4 successes for v2 with member 12345, 1 of 1
  for v1. Phase 9 measures this properly and writes `stability`.
- One live model call was spent on the recorder's structured naming step, which produced
  `get_member_savings_balance` and the description.

### How the core piece works  (plain English)

The artifact is the only thing that crosses between the model-driven half of the system and the
deterministic half, so this phase made it something both a person and a calling agent can read. It
now says what it does, what parameters it needs and which of those are secret, what it returns and
which step produced that, and for each step what was clicked or typed, how that element is described
in role-and-name terms, why the model did it in the model's own words, and what must be true
afterwards for the step to count as done. The recorder that builds it never runs during the discovery
loop. It is a separate pass over the written event log, which means recording cannot perturb the run
it is recording, and the same log can be re-recorded later by a better recorder. Two things generalise
the recording beyond the single run it came from: a value the model typed that matches a literal in
the goal becomes a declared parameter with the observed value kept only as an example, and a URL
containing that literal becomes a route pattern. Replay resolves both back from the caller's supplied
parameters, through one shared code path so a step value and a checkpoint can never disagree about
what a placeholder means.

### Decisions logged

- `docs/adr/0012-schema-richness-recorder-as-a-pass-and-field-marking.md`
- `docs/adr/0013-replay-interpolates-params-and-the-single-recording-limit.md`
- `ARCHITECTURE.md` — Phase 8 decisions, items 62 to 68.

### Caveats / not done

- **The capability generalises across members in its parameters but not yet in its locators.**
  Measured: replaying with `member_id=22222` fails at step 5 with `locator_unresolved`, because the
  recorded target name is the literal `"12345 - Testuser Alpha"` while the real page renders
  `"22222 - Sample Bravo"`. Canonicalization rewrites typed values and URLs but not a `describe()`
  derived accessible name that happens to embed the same digits. `frame_path` has the same issue
  (`"/member/12345/balance"`). This is the most interesting limit the phase found and it is a real
  piece of design work, not a typo: it needs locator-side canonicalization plus a `name_match="regex"`
  form. Documented in ADR 0013. I chose not to expand an already three-round phase into it.
- **The success checkpoint pins one observed value** (`$1,204.55`), so a correct replay reading a
  different member's balance would be reported as a failure. ADR 0013 states what a better success
  condition needs: a checkpoint that asserts a pattern, or the recognition that for a "read a value"
  goal the honest signal is that the declared output was extracted at all.
- **DoD 6's literal grep is not clean, but the defect it targets is fixed.** The only string in the
  file with more than four decimal places is the ISO timestamp's microsecond field
  (`2026-08-17T18:59:05.189651+00:00`). That is not float noise and I chose not to truncate a
  standard timestamp to satisfy a regex. The confidence values, which were the actual defect, are
  clean.
- **`code_sha` is null and will stay null.** Nothing populates it because this project is forbidden
  from running git by CLAUDE.md, so there is no commit hash available to the code. The field is in the
  schema because a real deployment would fill it.
- **`precondition` is null on every step**, `tenant_id` and `app_fingerprint` are null (Phase 12), and
  `stability` is null (Phase 9 writes it from its five-run replay).
- **"The resolution rank achieved at record time" is not independently verified.** The log carries no
  per-turn observation to re-resolve against, so the recorder parses the descriptor `describe()`
  already computed live rather than recomputing it. Disclosed in the recorder docstring and ADR 0012.
- **Latent recorder issues accepted and documented, not fixed:** `checked_urls` means the destination
  for a Navigate and the loaded frame URLs otherwise, so a postcondition derived across that boundary
  would assert a URL that only loads on the next step; `canonicalize_route` is not applied to a
  navigate step's own URL; and two different literals typed into equally-named fields collapse into
  one InputParam. None is reachable in any capability recorded so far, since none has an interior
  navigate step. A comment now sits at the derivation site.
- **A test expectation was changed, and I checked it rather than accepting it.** The pruning test's
  `[A,B,C,A,D]` case expected `[A0, D4]`; it now expects `[A3, D4]`. The old expectation kept the
  action whose own effect started the dead end, which would replay the detour. The new one is correct
  and the old one encoded the bug.
