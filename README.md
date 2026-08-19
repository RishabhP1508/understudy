# Understudy

An understudy learns a part by watching once, then performs it exactly, and never improvises.
That is discovery and replay in this project: a language model drives a real browser once to work
out how to do something in an application with no API, the successful run is recorded as a typed,
versioned capability, and every later invocation replays that capability deterministically, with
no model anywhere in the decision loop.

## Setup

Requires Python 3.11, pinned deliberately: the machine default on a recent install is often newer,
and part of this stack does not have wheels for it yet (`docs/adr/0001`). If `python --version`
reports anything other than 3.11.x, install 3.11 separately and point the venv creation step at it.

```bash
py -3.11 -m venv .venv                     # Windows
# python3.11 -m venv .venv                 # Linux/macOS
.venv/Scripts/pip install -e ".[dev]"      # Windows
# .venv/bin/pip install -e ".[dev]"        # Linux/macOS
.venv/Scripts/playwright install chromium  # Windows
# .venv/bin/playwright install chromium    # Linux/macOS
copy .env.example .env                     # Windows
# cp .env.example .env                     # Linux/macOS
```

There is no console script this project installs; `pyproject.toml` declares no `[project.scripts]`
entry point. Every command below runs as a module:

```
.venv/Scripts/python.exe -m understudy.cli <command> # Windows
.venv/bin/python -m understudy.cli <command> # Linux/macOS
```
**Every example in this file uses the Windows venv path, `.venv/Scripts/python.exe`.
On Linux and macOS substitute `.venv/bin/python` throughout.** Nothing else changes except
the `--params` quoting, which is shown per shell at every example that passes JSON.

`.env` holds two things that matter. `GEMINI_API_KEY` is required only for `discover` (and for
`record` when it wants to name the capability with a real model call instead of falling back to
the goal text). `GEMINI_MODEL` selects which model runs discovery and defaults to
`gemini-3.1-flash-lite`; the free tier is 20 requests per day, per model, and one discovery run
costs 8 to 9 requests, so switching the model opens a fresh daily budget if you run out
(`docs/adr/0003`). Nothing else in `.env.example` needs changing to run the demo path below.

## Running without live services

Everything except `discover` runs with no API key and no network access at all.

**The fixture app** is a local Flask process, not a hosted service. Start it in its own terminal:

```bash
.venv/Scripts/python.exe -m fixtures.legacy_bank --port 5055
```

It serves `http://127.0.0.1:5055`, deliberately built as a hostile legacy back office: an HTML
4.01 frameset, no ARIA, no `<label for=>`, table-derived captions, and an `<input type="button">`
submit control with an inline `onclick`. It has to stay running for anything below that touches a
real browser (`replay`, the escalation demo, and every live test). "No live services" means no
external network call and no LLM, not "nothing running locally": the fixture is local, and so is
the browser Playwright drives against it.

**The whole replay path** needs no key. Given an artifact and parameters, `replay` drives the
fixture with Playwright and returns a typed result with zero model tokens spent. See the demo path
below for two worked examples, one that succeeds and one that returns a business outcome.

**The escalation demo** (same section, below) needs no key either: it exercises the policy gate's
refusal of an irreversible action and the operator console's approval endpoint, with no model
involved on either side.

**The full test suite** runs offline with the fixture up:

```bash
.venv/Scripts/python.exe -m pytest -q
```

No test in this suite calls a live model. Every test that drives the real browser skips loudly,
naming the reason, if the fixture is not reachable on `127.0.0.1:5055`. Passing exit code with the
fixture running and zero skips is 330 tests as of this writing.

## The demo path

Start the fixture first, in its own terminal, and leave it running for everything below:

```bash
.venv/Scripts/python.exe -m fixtures.legacy_bank --port 5055
```

**1. Discover, with both a goal and a target.** This is the one step that spends real LLM quota
and takes about a minute and a half; the artifacts and evidence it produces already ship in this
repository (`artifacts/`, `evidence/discovery/`), so you do not have to run it to see the rest of
the demo work.

```bash
.venv/Scripts/python.exe -m understudy.cli discover \
  --goal "Look up member 12345 and read their current savings balance" \
  --target http://127.0.0.1:5055/login
```

Prints the run's step count, rejected-turn count, token usage, and the artifact path it wrote
under `artifacts/`. The shipped run took 8 turns, 98.0 seconds, and 22,422 model tokens, with zero
rejected turns, ending `goal_verified` with `savings_balance = $1,204.55`.

**2. Replay the resulting artifact.** Read the artifact's own declared inputs before writing
`--params`; do not guess them. The shipped balance-lookup artifact declares `password` (a secret
string) and `member_id` (an integer):

bash:
```bash
.venv/Scripts/python.exe -m understudy.cli replay \
  --artifact "artifacts/look-up-member-12345-and-read-their-current-savings-balance.v3.json" \
  --params '{"password": "demo-pass-1", "member_id": 12345}'
```

PowerShell (a native executable receiving a single-quoted argument from PowerShell needs its
embedded double quotes escaped, or they are stripped before Python ever sees them; this exact form
was run and confirmed against this repository):

```powershell
.venv\Scripts\python.exe -m understudy.cli replay `
  --artifact "artifacts\look-up-member-12345-and-read-their-current-savings-balance.v3.json" `
  --params '{\"password\": \"demo-pass-1\", \"member_id\": 12345}'
```

Prints a JSON result with `"kind": "success"` and `"savings_balance": "$1,204.55"`, in 7 steps and
about 2 seconds, spending zero model tokens. Exit code 0.

**3. Replay the not-found case.** Same artifact, a member id the fixture has no record of. This is
a business outcome, not a failure, and the exit code says so:

bash:
```bash
.venv/Scripts/python.exe -m understudy.cli replay \
  --artifact "artifacts/look-up-member-12345-and-read-their-current-savings-balance.v3.json" \
  --params '{"password": "demo-pass-1", "member_id": 99999}'
```

PowerShell:
```powershell
.venv\Scripts\python.exe -m understudy.cli replay `
  --artifact "artifacts\look-up-member-12345-and-read-their-current-savings-balance.v3.json" `
  --params '{\"password\": \"demo-pass-1\", \"member_id\": 99999}'
```

Prints `"kind": "business_outcome"`, `"code": "member_not_found"`, and exits 0. The application
correctly has no such member; that is an answer the capability's caller needs, not a broken run.

**4. The operator console and the escalation demo.** Start the console in one terminal:

```bash
.venv/Scripts/python.exe -m understudy.cli operator --port 8765
```

In a second terminal, replay the subaccount-opening capability, which is still `status: draft`
because its last step is irreversible and has had no human sign-off. Replaying it hits that step,
and the policy gate refuses it and raises an intervention instead of running it:

bash:
```bash
.venv/Scripts/python.exe -m understudy.cli replay \
  --artifact "artifacts/open-a-new-savings-subaccount-for-member-12345-with-the-nickname-vacation-fund-and-an-initial-deposit-of-250.v2.json" \
  --params '{"password": "demo-pass-1", "member_id": 12345, "initial_deposit": 250}' \
  --intervention-ttl 180
```

PowerShell:
```powershell
.venv\Scripts\python.exe -m understudy.cli replay `
  --artifact "artifacts\open-a-new-savings-subaccount-for-member-12345-with-the-nickname-vacation-fund-and-an-initial-deposit-of-250.v2.json" `
  --params '{\"password\": \"demo-pass-1\", \"member_id\": 12345, \"initial_deposit\": 250}' `
  --intervention-ttl 180
```

The command prints an intervention id and blocks. Open `http://127.0.0.1:8765/` in a browser (the
same machine, any browser you like; this is a separate, plain console, not the Chromium window the
run itself drives) and you will see the pending request, with its reason and a masked screenshot.
Click Approve. The blocked `replay` command resumes on its own, re-dispatches exactly the refused
click, and finishes with `"kind": "success"`. This is the same mechanism, run for real, that
produced `artifacts/open-a-new-savings-subaccount-...v2.json` in the first place: that capability
could not have been recorded at all without a human approving this exact refusal once during
discovery.

## Every command

There is no console script (see Setup); every example below is `understudy.cli <command>` run as
`python -m`.

**`discover --goal ... [--target ...]`** runs the LLM-driven observe/decide/act loop against a
live target and, if the goal is verified, writes a new capability version. `--goal` is required;
`--target` defaults to the policy's own `entry_point`. See the demo path above for a full example.

**`replay --artifact ... --params ...`** executes a recorded capability with no model in the loop.
`--artifact` and `--params` are both required. Exit code 0 covers both success and a business
outcome; 1 is a hard failure; 2 is a caller error (missing required params, or an artifact naming
a detector this build does not implement). See the demo path above for both a success and a
business-outcome example. `--overlay overlays/tenant_b.json` resolves the capability against a
tenant overlay first, in memory, without touching the file on disk (Heterogeneity, `REPORT.md`).
`--repeat N` runs replay N times and writes a read-only stability signal into the artifact.

```bash
.venv/Scripts/python.exe -m understudy.cli replay \
  --artifact "artifacts/look-up-member-12345-and-read-their-current-savings-balance.v3.json" \
  --params '{"password": "demo-pass-1", "member_id": 12345}' \
  --overlay overlays/tenant_b.json
```

**`record --run-dir ...`** rebuilds a capability from an evidence directory a `discover` run
already wrote, as a separate pass over `run.jsonl`. Useful for regenerating an artifact after a
recorder improvement, without spending a fresh model call:

```bash
.venv/Scripts/python.exe -m understudy.cli record --run-dir evidence/discovery
```

**`operator [--port]`** serves the plain FastAPI console a human uses to see pending interventions,
take control of a stuck run's live browser session, and approve or reject a refused risky action.
See the demo path above.

**`catalog [--transport stdio]`** publishes every capability under `artifacts/` as an MCP tool over
stdio, so a separate calling agent can discover and invoke them by name with typed arguments,
rather than a human already knowing an artifact's path and shape:

```bash
.venv/Scripts/python.exe -m understudy.cli catalog
```

`python -m understudy.catalog.demo` is a real MCP client, over stdio, against a real `catalog`
subprocess: it lists both capabilities, invokes the balance lookup for a real and a nonexistent
member, invokes the still-draft subaccount capability (refused), approves a copy of it, and
invokes it again, which escalates because the catalog itself can never pass `allow_risky=True` or
approve its own capability. It writes its own copy of `artifacts/` under
`evidence/catalog-invocation/` and never touches the real one:

```bash
.venv/Scripts/python.exe -m understudy.catalog.demo
```

**`approve --artifact ...`** is the human sign-off a catalog server can never perform on itself: it
flips a capability's `status` from `draft` to `approved` and writes the file back. This is the seam
that keeps a calling agent from ever being able to approve, or quietly arm, its own irreversible
action. It writes the file it is pointed at, in place, so the example below runs it against a copy
in a temp directory (`/tmp` on Linux/macOS, `%TEMP%` on Windows), not a shipped artifact:

```bash
copy artifacts\open-a-new-savings-subaccount-for-member-12345-with-the-nickname-vacation-fund-and-an-initial-deposit-of-250.v2.json %TEMP%\subaccount-copy.json
.venv\Scripts\python.exe -m understudy.cli approve --artifact %TEMP%\subaccount-copy.json
```

**`fingerprint --artifact ...`** computes a structural signature of an artifact's own entry screen
from a live observation, for an artifact recorded before this field existed, and writes it back.
It also writes the file it is pointed at, in place, so this example runs it against a copy too:

```bash
copy artifacts\look-up-member-12345-and-read-their-current-savings-balance.v3.json %TEMP%\balance-copy.json
.venv\Scripts\python.exe -m understudy.cli fingerprint --artifact %TEMP%\balance-copy.json
```

**`drift [--evidence-dir evidence]`** reports, per run and per step under an evidence directory,
which locator strategy actually resolved a target and at what rank, compared to the rank recorded
at discovery time. It is a plain text report, never a gate:

```bash
.venv/Scripts/python.exe -m understudy.cli drift --evidence-dir evidence
```

Run against this repository's own `evidence/`, it reports 14 runs, most resolving at rank 1
throughout, and two steps in `cross-tenant/tenant-b` resolving at rank 5 (`role_ordinal`) instead
of the recorded rank 1, which is the ranked locator strategy absorbing a real tenant rename the
overlay deliberately leaves undeclared (`REPORT.md`, Heterogeneity & multi-tenant).

## Evidence

`evidence/README.md` walks through what each recorded run demonstrates, including the two runs
worth reading first: the subaccount capability's own discovery, which could not have completed
without the escalation in the demo path above, and a session-expiry replay that a human resolved
by logging back in on the same live browser session.

## What is real and what is mocked

| Component | Status |
|---|---|
| Browser control (headed Chromium via Playwright) | Real |
| Accessibility-tree perception | Real |
| The LLM in discovery | Real (Gemini, free tier) |
| The MCP protocol in the catalog | Real (a genuine stdio client and server) |
| Same-session human handoff | Real |
| The target application | A local Flask fixture (`fixtures/legacy_bank/`), deliberately hostile |
| The operator console | Deliberately plain; a working console, not a polished one |
| The second tenant | A blueprint under `/tenantb` of the same fixture process |
| The desktop surface | A documented, non-functional seam (`src/understudy/surface/desktop_stub.py`) |
