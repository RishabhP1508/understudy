# Evidence

Every file here is the output of running the system. Nothing in this directory was written by hand.

## Read these two first

**`discovery-subaccount/`** is the strongest single piece of evidence in the repository. An LLM drove
the fixture app to open a savings subaccount, hit a step the policy gate classified as irreversible,
stopped, and raised an intervention. A human approved it in the operator console, the run resumed on
the same browser session, and the capability was recorded. The intervention record for that approval
is `esc529e93616a.json`, in the same directory. Without it, `artifacts/open-a-new-savings-subaccount-...v2.json`
would not exist.

**`escalation/`** is the other half of the human-in-the-loop story: a replay whose session expired
mid-flow, handed control to a human who logged back in by hand, and resumed to a correct answer. Its
intervention record (`escc59ee3e456.json`) holds 19 recorded human actions and the custody chain
across all four control states, `automation` to `pending_handoff` to `human` to `pending_resume` and
back to `automation`, each transition naming the actor that made it. The run log shows the same arc
from the automation side, including a `step_skipped_after_handoff` event where the resume logic
re-checked the step's postcondition, found the human had already done that step's work, and skipped it
instead of repeating it.

## The eleven runs

| directory | what it shows | outcome | steps | wall |
|---|---|---|---|---|
| `discovery/` | LLM discovery of the balance lookup | goal_verified | 8 | 98.0s |
| `discovery-subaccount/` | LLM discovery that escalated and was approved | goal_verified | 11 | 51.6s |
| `replay-success/` | deterministic replay, member 12345 | success, `$1,204.55` | 7 | 2.2s |
| `replay-different-member/` | same artifact, member 22222 | success, `$532.10` | 7 | 2.1s |
| `replay-business-outcome/` | member 99999 | business_outcome, `member_not_found` | - | 1.8s |
| `replay-recovered/` | native browser dialogs, dismissed | success, 3 recoveries | 7 | 2.4s |
| `replay-retried/` | transient 503s, retried | success, backoff 250ms then 500ms | 7 | 3.2s |
| `replay-hard-failure/` | app error page | hard_failure, `app_error` | - | 1.4s |
| `escalation/` | session expired, human took control, run resumed | success | 7 | 43.5s |
| `cross-tenant/` | one recording against two tenants | both success | 10 and 11 | 2.9s, 3.0s |
| `catalog-invocation/` | the capabilities called as MCP tools | see below | | 41.6s |

`replay-different-member/` is the one to compare against `replay-success/`: same artifact, same steps,
a different typed input, a different answer. That is what makes the recording a capability rather than
a script.

`cross-tenant/` holds `tenant-a/` and `tenant-b/` side by side, plus the exact capability copy that was
replayed. Both runs used the same recording. Tenant B needed only `overlays/tenant_b.json`, a 34 line
file. The tenant B run logs two `locator_drift` events, at steps 0 and 8, where a tenant rename was
absorbed positionally by the ranked locator instead of being declared in the overlay.

`catalog-invocation/transcript.jsonl` is a transcript of an AI agent discovering both capabilities over
MCP and calling them by name. Act 3 returns a business outcome as a normal result rather than an error.
Act 4 refuses a draft capability. Act 6 invokes the capability whose last step is irreversible: the
catalog escalates to a human instead of performing it, and returns the intervention id to the caller.

`artifacts/` holds the two published capabilities and the tenant B overlay, so the artifacts a reviewer
reads sit next to the runs that produced and consumed them.

## What a human types into a password field is never captured

Open `escalation/escc59ee3e456.json` and read `resolution.human_actions`. It has 19 entries. The
eight for the password field carry the value `[SUPPRESSED]`. The entries for the username field
beside them carry the real keystrokes the operator typed, `a`, `ad`, `adm`, `admi`, `admin`, which is
how you can tell this is suppression of one field rather than a blanket wipe of the record. The action
itself is still there in every case, because "the human typed into the password field" is exactly what
R6 asks the system to record; only the value is gone.

Suppression happens at capture, not by redaction afterwards, and the difference is the whole point.
A handoff records DOM input events, so a password arrives one keystroke at a time: `h`, `hu`, `hun`,
and so on. Redacting the value after the fact would mask the final entry and leave every prefix
sitting in the record, which reconstructs the password exactly. So `WebSurface`'s injected listener
writes the sentinel in place of the value before it ever leaves the page, for any
`input type="password"`, and `drain_human_actions` applies the project's own
`classify_field_sensitivity` as a second layer for a sensitive field name that is not a password
input. Two live tests cover the two layers, each verified to fail with its own line removed.

Neither of the two redaction rules could have caught this, and both are behaving as designed. The
first replaces values registered as declared secret parameters, and a value a human types during a
handoff was never a declared parameter. The second redacts credential-shaped literals, and an
ordinary word typed into a password box carries no credential keyword. That is why the fix belongs at
the capture boundary rather than in the serializer.

The screenshot in the same record is masked over the password field, as it was before. Visual and
structured evidence now agree.

## What CI proves, and what it does not

`.github/workflows/ci.yml` runs `pytest` on Linux with no browser installed and no fixture server
running, so every live test skips there. A green badge proves the offline half of the suite passes.
It proves nothing about the browser automation, and none of the runs in this directory came from CI.

Every artifact here was produced locally, against the fixture app on `127.0.0.1:5055`, with a headed
browser. The full suite is 328 tests and passes with zero skips only when that fixture is running.

## Notes for reading these files

Directory names are descriptive. The run id inside each `run.jsonl` is what identifies a run, and the
phase reports under `docs/reports/` cite the older, run-id-based directory names from before this
curation.

Every `run.jsonl` ends with a terminal marker: `replay_end` carrying the result kind for a replay,
`run_end` for a discovery run. Every run also writes `result.json`. Playwright traces are excluded by
`.gitignore` because they are large and regenerable.

Two limits are worth knowing before drawing conclusions from `cross-tenant/`. The business outcome
detectors match on the application's own wording, and tenant B is detected only because it happens to
render one of the strings the detector already scans for. The `app_fingerprint` comparison hashes the
entry screen's structure including its title, so it always differs across tenants and is a
within-tenant version signal rather than a cross-tenant one. Both are covered in REPORT.md.

Curation reduced this directory from 59 directories and 11MB of working output to 12 entries and
3.8MB on disk, of which 2.6MB across 262 files is what actually gets committed, since `.gitignore`
excludes the Playwright traces.
