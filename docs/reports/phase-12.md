## Phase 12 Verification Report

Status: COMPLETE

Loop summary: three rounds. Round 1 built tenant B (task A) and the overlay/drift/fingerprint work
(task B). Round 2 fixed the one real defect, which I found by running the headline replay myself: the
cross-tenant replay failed at step 7's postcondition, because a rename that the LOCATOR absorbs
positionally is not a rename a CHECKPOINT absorbs at all. Round 3 pinned what the clause-2 live test
actually demonstrates (it was passing on a run that fails), made the fingerprint warning say in words
that it is a warning, renamed a colliding function, and fixed the sibling half of a tenant bug the
task-A builder had flagged and left.

Delegation: the builder wrote tenant B's blueprint and nine templates, the tenant B policy, the
TenantOverlay model and `resolve_for_tenant`, the fingerprint function and its three call sites, the
drift report, three CLI commands and all 44 phase tests. The main session measured the tenant B
surface with the project's own perception BEFORE the overlay was written (so the overlay was designed
against real resolution data rather than guesses), set the drift-clause and vocabulary-table designs,
ran every live cross-tenant replay itself, and found the checkpoint asymmetry.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] **1. THE HEADLINE: the tenant A recording replays against tenant B with only an overlay.**
  Ran, against a fixture restarted after the last fixture change:

      $ understudy replay \
          --artifact evidence/tenant-b/artifacts/subaccount.v2.json \
          --params '{"member_id":12345,"password":"...","initial_deposit":250}' \
          --policy policies/legacy_bank_tenant_b.yaml \
          --overlay overlays/tenant_b.json \
          --allow-risky --no-escalate --evidence-dir evidence/tenant-b
      {"kind": "success", "outputs": {}, "steps_run": 11, "duration_ms": 2217.99}

  11 steps: the 10 recorded against tenant A, plus the one the overlay inserts for tenant B's extra
  review screen. Nothing was re-recorded and no model ran. The tenant A baseline, same artifact and
  same params with tenant A's policy and no overlay, returns `success` in 10 steps.
  The artifact is an approved, fingerprinted COPY of the subaccount capability: the capability's last
  step is irreversible, so it needs `status: approved` plus `--allow-risky`, and the repository's own
  copy stays `draft` on purpose (Phase 11's decision). The copy was made with `approve` and
  `fingerprint`, both real commands, not by editing the file.

- [x] **2. The overlay is small and reviewable.** `wc -l overlays/tenant_b.json` -> 34 lines. Printed
  in full under "The overlay" below. HUMAN-REVIEW for readability; the line count is machine-checked.

- [x] **3. An overlay naming a nonexistent step is rejected.** Ran the CLI with an overlay whose
  `step_overrides` names step "42":

      invalid overlay: step_overrides names step id '42', which does not exist in this capability
      exit=2

  No evidence directory was created: the overlay is validated before a browser is launched.

- [x] **4. An overlay changing a step's action type is rejected.**

      invalid overlay: step_overrides for step '9' would change its action from 'click' to 'type',
      which is not allowed
      exit=2

- [x] **5. `app_fingerprint` differs between tenants and the mismatch warns.** From the two runs'
  own `run.jsonl`:

      tenant A run: {"status": "match",
                     "actual": "a0424bd422dcdc471193e923adad4c4c5e2b9e05b86f134d5811358a0d8af024"}
      tenant B run: {"status": "mismatch",
                     "recorded": "a0424bd422dcdc471193e923adad4c4c5e2b9e05b86f134d5811358a0d8af024",
                     "actual":   "eff15870aebceadb2d259e77be80fe0a2067078b176273206c43445cd801e776",
                     "note": "the entry screen's structure differs from the recording (frame count,
                              control mix, or title/heading text changed); this is a warning only
                              and the run continued"}

  Both runs ended `success`. The mismatch changed no result.

- [x] **6. The drift report runs over evidence and produces per-step rank data.**
  `understudy drift --evidence-dir evidence/tenant-b`, verbatim, both runs:

      run: replay-id9105ea358321          (tenant A)
        step 0..9: recorded_rank=1 actual_rank=1 strategy=role_name_exact drift=none
        rank distribution: rank 1: 10

      run: replay-ideeef743abda6          (tenant B, via the overlay)
        step 0:  recorded_rank=1 actual_rank=5 strategy=role_ordinal
                 drift=rank_regressed+name_no_longer_matched
        step 1..7, 9: recorded_rank=1 actual_rank=1 strategy=role_name_exact drift=none
        step 8:  recorded_rank=1 actual_rank=5 strategy=role_ordinal
                 drift=rank_regressed+name_no_longer_matched
        step 10: recorded_rank=None actual_rank=1 strategy=role_name_exact drift=none
        rank distribution: rank 1: 9, rank 5: 2

  Step 10 is the overlay's inserted step: `recorded_rank=None` because no recorder ever measured it,
  and the report prints that rather than imputing a rank. Run over the curated `evidence/` directory
  instead, it names every older run as carrying no rank data rather than inventing one.

- [x] **7. Which clause fires for which difference.** Its own section below, with measured output.

- [x] **8. Tenant B is as hostile as tenant A.** `test_template_has_no_modern_test_hooks` is
  parametrized over BOTH tenants' template directories, so tenant B is held to tenant A's bar and a
  failure names the offending file: no `data-testid`, no `aria-label`, no `<label for=`. Plus a
  frameset assertion per tenant. Tenant B is a frameset shell over `<table>`/`<font>` markup with
  inline `onclick="this.form.submit()"` and meaningless field names (`uid`, `pwd`, `nick`, `dep`).

- [x] **9. The injection modes still arm for tenant B.** Against the live server, not the test
  client: arm through `/tenantb/login?inject=<mode>`, log in, request a tenant B page.

      not_found         -> "No matching record was found"
      permission_denied -> "Permission denied for this operation"
      control (no injection armed) -> "Testuser Alpha"

  Two bugs had to be fixed for this to be true: `/tenantb/login` was not in `EXEMPT_PATHS` (an armed
  mode would have applied to tenant B's own login page), and both the `session_expired` branch and
  the shared `require_login` decorator redirected a tenant B session to TENANT A's login. Phase 9's
  table is therefore not tenant-A-only.

- [x] **10. Rank distribution, tenant A versus tenant B.** In its own section below.

- [x] **11. ADR.** `docs/adr/0018-tenant-overlays.md`, eleven decisions including the required
  interpretation: the same step degrading the same way across many tenants means the vendor shipped a
  version; one tenant degrading alone means a local config change.

- [x] **12. Full suite under BOTH invocations, fixture live, zero skips.**

      $ .venv/Scripts/pytest.exe            (the console script; what CI runs)
      324 passed in 106.45s

      $ .venv/Scripts/python.exe -m pytest
      324 passed in 106.73s

  `addopts` carries `-rs`, so any skip would print its reason; none did. 310 before task B, 324 now.
  `ruff check .` -> All checks passed! `mypy src/` -> Success: no issues found in 41 source files.

- [x] **13.** This file.

### The overlay  (34 lines, the whole thing)

    {
      "tenant_id": "tenant_b",
      "base_capability_id": "open-a-new-savings-subaccount-for-member-12345-...",
      "base_version": 2,
      "entry_point_override": "http://127.0.0.1:5055/tenantb/login",
      "vocabulary_map": {
        "Member ID": "Customer ID",
        "Subaccount": "Linked Account",
        "Submit": "Continue",
        "/members": "/tenantb/customers",
        "?f7=": "?q=",
        "/member/": "/tenantb/customer/",
        "/subaccount/": "/linked-account/"
      },
      "step_overrides": {
        "7": {"postcondition": {"kind":"element_present","target":"textbox","value":"Opening Deposit"}},
        "9": {"postcondition": {"kind":"element_present","target":"button","value":"Confirm"}}
      },
      "extra_steps": [ { "after_step_id": "9", "step": { ...click button "Confirm"... } } ],
      "notes": "..."
    }

One substitution table covers three kinds of tenant difference at once: a renamed label
("Member ID"), a renamed route segment ("/member/"), and a renamed query parameter ("?f7="). They are
the same kind of fact, told once. `"Subaccount" -> "Linked Account"` alone fixes both the
"Open Subaccount" link and the "Subaccount Opened" success checkpoint.

### Which clause fires for which tenant difference  (DoD 7)

Measured, not reasoned. Three outcomes, and the third is the one worth reading.

**A rename WITH a recorded ordinal: absorbed positionally, drift reported.**
"Username" -> "User ID" (step 0) and "Initial Deposit" -> "Opening Deposit" (step 8) are deliberately
left undeclared by the shipped overlay. Both descriptors carry an ordinal, so the ranked locator falls
through to `role_ordinal` and resolves them at rank 5. Which clause names it depends on the artifact:

  - On the SUBACCOUNT capability (`recorded_rank: 1` on all ten steps), the measured clause is
    `rank_regressed+name_no_longer_matched`. Both facts are true, so both are reported. Before this
    phase only the first was, which hid the more diagnostic half: that the recorded NAME stopped
    matching is the tenant-vocabulary case, and rank alone does not say it.
  - On the BALANCE capability (`recorded_rank: null` on all seven steps, recorded before that field
    existed), the same difference at step 0 measures as `name_no_longer_matched` ALONE, because
    clause 1 cannot fire without a baseline. This is exactly why clause 2 exists, and it is live
    evidence, not an argument: `test_live_..._absorbs_one_rename_and_fails_on_the_other` drives a real
    browser against tenant B and asserts that clause.

  So "Member" becoming "Customer" is clause 2's case on an artifact with no measured rank, and both
  clauses on one that has it. The phase prompt expected clause 2; the difference is precedence, not
  detection, and reporting both is what makes the prompt's expectation true on both artifacts.

**A rename WITHOUT a recorded ordinal: no drift signal at all, and a loud failure instead.**
"Member ID" -> "Customer ID" at step 3. That descriptor has no ordinal, so when the name stops
matching there is nothing weaker to fall through to. Measured, from the same live run:

    "category": "locator_unresolved", "step_id": 3,
    "expected": "a unique element matching role='textbox' name='Member ID'",
    "observed": "... role_ordinal: 1 candidate(s) (descriptor recorded name='Member ID', which
                 matched no element; the sole remaining role='textbox' candidate is not used to
                 rescue a descriptor that once had a meaningful name); ..."

No `locator_drift` event is emitted, because nothing resolved. This is a difference the overlay MUST
declare, and it is the honest answer to "say so plainly": the run stops with a debuggable error
rather than passing in silence. The resolver deliberately refuses to rescue a once-named descriptor
by position alone, which is why this fails instead of quietly typing into the wrong box.

**Differences that produce no signal, correctly.** Tenant B's different CSS class names, different
form field `name` attributes, one extra level of table nesting, and its "NorthBay CU :: ..." title
pattern produce no drift signal whatsoever, because nothing in the artifact ever referred to them.
That silence is the design working: the recorded flow names roles and accessible names, never markup.
The one structural difference that IS caught is caught by a different mechanism entirely, the
`app_fingerprint` mismatch, which is why that field exists.

### The defect I found by running it  (worth reading before the next phase)

A rename a LOCATOR absorbs is not a rename a CHECKPOINT absorbs. `resolve()` walks six ranked
strategies, so step 8's descriptor survived "Initial Deposit" -> "Opening Deposit" positionally.
`checkpoint_satisfied` has no fallback at all: one exact role-and-name match. So step 7's
postcondition, which names the SAME renamed field, failed hard and took the whole replay with it:

    "step_id": 7, "category": "postcondition_failed",
    "expected": "step 7 (type into 'Nickname'): a textbox named 'Initial Deposit' to be present"

The fix is one `step_overrides` entry, not a vocabulary entry, so step 8's descriptor stays
positionally absorbed and the drift case survives. The general rule, now in ADR 0018: an overlay must
declare any rename a checkpoint references, even when the locator could have absorbed it. Giving
checkpoints the same ranked matching is the obvious follow-up and is not in this phase.

### Invariants

    $ .venv/Scripts/pytest.exe tests/test_constraints.py -v
    tests\test_constraints.py .....                                          [100%]
    5 passed in 0.25s

None skipped. Note invariant 1 under this phase's change: `resolve_for_tenant` lives in
`models/artifact.py` and the overlay is resolved before replay begins, so the cross-tenant path adds
no import reachable from `replay/` to any model or provider.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

Resolution rank distribution, from the two live runs above. Both are the SUBACCOUNT capability, which
carries `recorded_rank: 1` on all ten recorded steps; the balance capability is excluded from this
distribution because its `recorded_rank` is null on every step and averaging across it would invent
a baseline.

    tenant A, no overlay:   rank 1: 10   (10/10 steps at role_name_exact)
    tenant B, via overlay:  rank 1: 9, rank 5: 2   (of 11 steps, including the inserted one)

Two of eleven steps degraded from rank 1 to rank 5, both by deliberate design of the overlay, both
reported. Replay duration was 2.09s for tenant A and 2.22s for tenant B, so the cross-tenant path
costs nothing measurable. The balance capability against tenant B, with a minimal overlay, drifts at
step 0 (rank 5, clause 2) and then fails at step 3 as described above.

### How the core piece works  (plain English)

An overlay is a small JSON document that sits next to a recording and says how one tenant of the same
vendor product differs. Most of it is a single vocabulary table: this tenant calls "Member ID"
"Customer ID", keeps its customers under "/tenantb/customer/" instead of "/member/", and names its
search parameter "q" instead of "f7". At invoke time, and only in memory, that table is applied in one
pass to every string the recording holds that a tenant can reword: each target's name, its relational
label, its frame path, and every checkpoint value. Two escape hatches cover what wording cannot reach:
a step override replaces one step's assertion when the tenant genuinely checks something different,
and an extra step splices in a screen this tenant has and the original does not, which is how tenant
B's review-then-confirm flow works from a recording that only ever saw one submit button. The result
is an ordinary Capability the replay engine runs exactly as it runs any other, with no model involved,
and it is never written to disk as a recorded artifact, so there is still one recording per capability
rather than one per tenant. What the overlay does not declare, the ranked locator tries to absorb on
its own, and anything it absorbs on weaker evidence than it was recorded with is reported as drift, so
the gap between what the overlay claims and what the tenant actually is stays visible instead of
silently accumulating.

### Decisions logged

- docs/adr/0018-tenant-overlays.md — eleven decisions: overlay over per-tenant recording, one
  vocabulary table rather than separate label/route/checkpoint fields, why the shipped overlay leaves
  two renames undeclared, the locator/checkpoint asymmetry, the vendor-version versus local-config
  interpretation of drift, why the fingerprint warns and never gates, and why `_drift_reason` now
  returns every applicable clause.
- ARCHITECTURE.md decisions 91 to 98.

### Caveats / not done

- **`app_fingerprint` is captured live by a command, not yet by a real recording.** The two artifacts
  in `artifacts/` were recorded before the field existed and their discovery evidence saved no
  observation snapshots, so the recorder cannot derive it retroactively; `understudy fingerprint`
  observes the entry screen live and writes it, which is how the tenant A baseline above got one. The
  loop-logs-it, recorder-reads-it path IS wired and covered by an offline test with a fake LLM, but no
  artifact in this repository has yet been produced through it, because that needs a fresh discovery
  run and this phase did not do one.
- **The business-outcome detectors survive the tenant change by luck, not by mechanism.** Tenant B's
  search miss renders "No matching record was found.", which happens to be one of the three needles
  `member_lookup_no_match` already scans for. A tenant that reworded it to "No customer matches that
  search" would not be detected, and the overlay has no field for detector vocabulary. `BusinessOutcome`
  is already shaped for this (stable `code` and `message`, tenant-specific `observed`), but the
  detection side is not, and that is a real gap in the multi-tenant story.
- **Only one overlay ships.** The repository layout names `overlays/tenant_b.json`, so the clause-2
  demonstration for the balance capability builds its overlay in memory inside a live test rather than
  adding a second file.
- **Tenant B shares tenant A's error templates** (not_found, permission_denied, error_generic,
  interstitial), on the argument that those are vendor-generic pages. It is also what keeps the Phase 9
  detector table working across both tenants, so the two facts are not independent.
- **Frame names are assumed stable across tenants.** Both tenants use `navframe` and `contentframe`; a
  tenant that renamed its frames would break every step whose `frame_path` names one, since frame paths
  have no ranked fallback. The vocabulary table does reach `frame_path`, so it is declarable, but it is
  untested against a tenant that actually does it.
