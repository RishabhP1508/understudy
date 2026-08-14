## Phase 1 Verification Report

Status: COMPLETE

Loop summary: 1 round. The builder delivered the fixture and all nine injection modes working on the
first pass, so there was nothing to send back. My verification found no defect in the fixture itself.
I did clean up two files that had landed outside the declared structure: `fixture_server.log`, which
the builder created when it backgrounded the server, and `.playwright-mcp/`, which my own browser
verification wrote into the repo root. The log is deleted and `.playwright-mcp/` is now gitignored,
since I will use that browser again in Phases 3, 4, and 12.

One dependency decision, flagged before delegating: the phase prompt specifies Flask, but Flask is not
in the dependency list CLAUDE.md's conventions establish. CLAUDE.md names no framework for the fixture,
so this is a gap rather than a contradiction. I checked whether an installed dependency could do the
job first, per the ponytail ladder: nothing was available (`flask`, `jinja2`, `itsdangerous`, and
`werkzeug` were all missing), and a FastAPI version would have needed jinja2 for templates plus
itsdangerous for signed session cookies, so it costs the same. `flask` was added to `[project]
dependencies`, not the dev extra, because the Phase 14 demo path has a reviewer run this app.

### Machine-checkable gate  (ALL green for COMPLETE)

- [x] the app serves — ran: `.venv/Scripts/python.exe -m fixtures.legacy_bank --port 5055` — got:
  `* legacy_bank fixture serving on http://127.0.0.1:5055` then Flask's usual startup lines — expected:
  starts without error. Smoke: `GET /login -> 200 | title: Legacy Bank - Login`, and `GET /app` with no
  session -> `302 /login`.
- [x] nine injection modes, nine pieces of real output — ran: my own script against the running server,
  each mode on a fresh logged-in session (full output below) — expected: each mode produces its stated
  behavior
- [x] transient_failure fails twice then succeeds, in one session — ran: `/admin/inject?mode=transient_failure`
  then three sequential GETs on one `requests.Session` — got: `attempts=[503, 503, 200]` and the third
  response is the real member page (`third-is-member-page=True`) — expected: 503, 503, 200
- [x] native_dialog is a real browser dialog — ran: page source check plus a real browser navigation —
  got: `window.confirm present=True`, `script="window.confirm('Are you sure?');"`, and in Chromium the
  navigation **timed out after 60s at `domcontentloaded`** until I answered the dialog with
  `browser_handle_dialog(accept=true)`, after which the page rendered as "Member Detail" — expected: a
  dialog that blocks and needs a handler
- [x] the happy path completes — ran: a scripted `requests.Session` from `/login` to the confirm screen
  — got: reference number `REF-BD646A9B` on the confirm page (full trace below) — expected: a reference
  number
- [x] no automation hooks in the markup — ran: `grep -rIn "data-testid\|data-test\|aria-label\|role="
  fixtures/legacy_bank/templates` — got: no output, exit 1 — expected: empty
- [x] and nothing similar sneaked in — ran: `grep -rIoEn "aria-[a-z]+|data-[a-z]+=|[^a-z]id="
  fixtures/legacy_bank/templates` — got: `(none)` — expected: no ARIA, no data attributes, no id hooks
- [x] at least three fields labeled only by adjacent cell text — ran: `grep -rIn "<label"
  fixtures/legacy_bank/templates` — got: `(no <label> element anywhere)` — expected: none, so all three
  qualify (markup quoted below)
- [x] the outer shell is a real frameset with no `<body>` — ran: the Playwright MCP browser against
  `/app` — got: `docElChildren: ["HEAD","FRAMESET"]`, `realBodyTags: 0`, `framesetCols: "160,*"`,
  2 frames — expected: a frameset document with no body
- [x] a nested `<iframe>` renders inside a frame — ran: a live frame-tree walk in the browser — got: a
  three-level tree, `/member/12345/balance` in an IFRAME inside a FRAME (below) — expected: nesting
- [x] no realistic PII — ran: a regex sweep for SSN, card number, email, and phone patterns across
  `fixtures/legacy_bank/` — got: `(no SSN, card number, email, or phone pattern anywhere in the fixture)`
  — expected: nothing
- [x] the external hop exists for the Phase 5 guard — ran: `GET /external` — got:
  `302 Location='https://example.com/'` — expected: a redirect to a genuinely external origin
- [x] previous phases still green — ran: `ruff check .`, `pytest -q`, `mypy src/` — got: `All checks
  passed!` (exit 0), 5 skips and exit 0, `Success: no issues found in 13 source files` — expected: no
  regression
- [x] docs/reports/phase-1.md saved — this file

### The nine injection modes, real output

Each on a fresh session, run by me against the live server, not quoted from the builder:

    1 validation        200  'Deposit amount could not be validated. Please re-enter.'
    2 not_found         200  'No matching record was found.'
    3 permission_denied 403  'Permission denied for this operation.'
    4 unexpected_dialog 200  dismiss-link=True  'A confirmation is required before continuing.'
      after dismiss     200  member-page=True
    5 native_dialog     200  window.confirm present=True  script="window.confirm('Are you sure?');"
    6 session_expired   302  Location='/login'
    7 slow_load         200  elapsed=6.00s
    8 transient_failure attempts=[503, 503, 200]  third-is-member-page=True
    9 app_error         500  'An unexpected error occurred.'

Both selection mechanisms work: `?inject=<mode>` for a single request (modes 1 to 7 and 9 above) and
`/admin/inject?mode=<mode>` for a persisted session mode (used for mode 8, then cleared with
`mode=none`). `/login` and `/admin/inject` are exempt from injection, so a stuck mode cannot lock you
out.

The two dialog modes are genuinely different code paths, which was the point of requiring both:
`unexpected_dialog` is an ordinary page that loads fine and is dismissed by following a link, so a DOM
interaction clears it. `native_dialog` blocked Chromium's navigation entirely until a dialog handler
answered it. Replay will need `page.on("dialog")` for one and a click for the other.

### Happy path, one scripted session

    POST /login                          -> 302 /app
    GET  /app  (frameset)                -> 200 frameset=True body_tag=False
    GET  /members?f7=12345               -> 200 found=True
    GET  /member/12345                   -> 200 iframe=True
    GET  /member/12345/balance (iframe)  -> 200 balance=$1,204.55
    GET  /member/12345/subaccount/new    -> 200
    POST /member/12345/subaccount/new    -> 200 final_url=.../subaccount/confirm?ref=REF-BD646A9B
         reference number on confirm     -> REF-BD646A9B

Seed-data outcomes that need no injection at all, which is what makes 55555 and 99999 useful as replay
targets later:

    GET /member/12345  -> 200  member page renders
    GET /member/22222  -> 200  member page renders
    GET /member/33333  -> 200  member page renders
    GET /member/55555  -> 403  You do not have permission to view member 55555.
    GET /member/99999  -> 200  No such member: 99999.

### The frameset, and a finding worth keeping

`templates/app_frameset.html` in full:

    <!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Frameset//EN" "http://www.w3.org/TR/html4/frameset.dtd">
    <html>
    <head><title>Legacy Bank Back Office</title></head>
    <frameset cols="160,*">
    <frame src="/nav" name="navframe">
    <frame src="/members" name="contentframe">
    <noframes><font size="2">This application requires a frames-capable browser.</font></noframes>
    </frameset>
    </html>

Browser-verified, and this is the part worth carrying into Phase 3. Querying the live document gave:

    bodyTagName: "FRAMESET"      bodyIsFrameset: true      realBodyTags: 0
    docElChildren: ["HEAD", "FRAMESET"]

There is no `<body>` element in the document, but `document.body` is still truthy, because the HTML
spec defines `document.body` as the first `body` **or frameset** child of `html`. So perception code
that guards with `if (document.body)` will sail straight through and then find nothing useful, which is
exactly the naive-snapshot failure this fixture exists to produce. `src/understudy/surface/web.py` has
to enumerate frames, not trust a body.

The live frame tree, walked in the browser after driving the content frame to the member screen:

    depth 0  /app                        top(frameset)   ""
    depth 1  /nav                        FRAME           "Legacy Bank Member Search"
    depth 1  /member/12345               FRAME           "Member ID 12345 Name Testuser Alpha Status Active..."
    depth 2  /member/12345/balance       IFRAME          "$1,204.55"

The savings balance is reachable only by traversing a FRAME and then an IFRAME. A single-document
snapshot never sees it.

### The three fields labeled only by adjacent cell text

All in `templates/subaccount_new.html`. There is no `<label>` element anywhere in the fixture, so the
only thing tying a caption to an input is that they sit in neighbouring `<td>` cells:

    <tr><td class="td3"><font size="2">Account Type</font></td><td class="td4">
    <select name="f1">
    <option value="SAV" ...>Savings</option>
    <option value="CHK" ...>Checking</option>
    </select></td></tr>
    <tr><td class="td3"><font size="2">Nickname</font></td><td class="td4">
    <input type="text" name="f2" value="{{ nickname }}"></td></tr>
    <tr><td class="td3"><font size="2">Initial Deposit</font></td><td class="td4">
    <input type="text" name="f7" value="{{ deposit }}">
    {% if deposit_error %}<font color="red">{{ deposit_error }}</font>{% endif %}
    </td></tr>

Account Type is `f1`, Nickname is `f2`, Initial Deposit is `f7`. The names are meaningless and
non-sequential on purpose. The submit control is
`<input type="button" value="Submit" onclick="this.form.submit()">`, which is nastier than the phase
prompt asked for and I am keeping it: it is not a submit input, so there is no implicit form
submission and nothing that only reads or posts HTML can get past this screen without executing JS.

### Seed data

Five members, each carrying only an ID, a display name, a status, and a balance. Nothing else exists on
the model, so there is no field that could hold PII even by accident.

    12345  Testuser Alpha    Active     $1,204.55   the happy path
    22222  Sample Bravo      Active     $532.10
    33333  Fixture Charlie   Inactive   $88.00
    55555  no name exposed, always 403 permission denied, no injection needed
    99999  no name exposed, always the not-found page, no injection needed

No SSN, address, date of birth, email, or phone number anywhere. The names are visibly fake. The
sentinel `123-45-6789` used by invariant 3 appears nowhere in the fixture, which the regex sweep
confirms.

### Invariants

    $ .venv/Scripts/python.exe -m pytest -q
    sssss                                                                    [100%]
    SKIPPED [1] tests\test_constraints.py:256: src/understudy/replay/ arrives in Phase 2; invariant 1 goes live then
    SKIPPED [1] tests\test_constraints.py:268: src/understudy/safety/policy.py (PolicyGate.dispatch) arrives in Phase 2; invariant 2 goes live then
    SKIPPED [1] tests\test_constraints.py:283: understudy.safety.redact.Redactor arrives in Phase 2 (stub) and Phase 5 (full); invariant 3 goes live then
    SKIPPED [1] tests\test_constraints.py:314: understudy.models.artifact.Capability arrives in Phase 2; invariant 4 then
    SKIPPED [1] tests\test_constraints.py:342: understudy.agent.tools.FINISH_TOOL arrives in Phase 2; invariant 5 then
    exit 0

Unchanged from Phase 0, which is correct: this phase added no code under `src/`. The fixture is not
covered by mypy (`files = ["src"]`) but is covered by ruff, and ruff is clean.

### Run-and-report numbers  (reported, NOT gated, never optimized against)

No model ran this phase, so there are no discovery or replay numbers. Fixture size: `app.py` is 299
lines, `__main__.py` is 17, and 12 templates come to 156 lines total. Nine injection modes, five
screens, three frame levels, five seed members.

### How the core piece works  (plain English)

The fixture is a small Flask app whose job is to be unpleasant in the specific ways a twenty year old
back office is unpleasant. The outer shell at `/app` is a genuine HTML 4.01 frameset, so the top
document has no body and its content lives in two child frames; the member screen inside the content
frame then embeds a further iframe holding the savings balance, so reading one number requires walking
two levels of frame boundary. Every screen is built from tables nested three deep with meaningless
class names, form fields are called `f1`, `f2`, and `f7`, captions are plain text in the cell next to
the input rather than a `<label>`, and the submit button is an `<input type="button">` with an inline
onclick, so nothing works without a real browser. Failure injection runs through a single Flask
`before_request` hook, chosen either per request with `?inject=<mode>` or persisted in the session
through `/admin/inject`, and it covers the nine runtime conditions replay has to survive. Two of those
deserve their separate existence: `unexpected_dialog` is an HTML interstitial cleared by clicking, and
`native_dialog` is a real `window.confirm()` that halts page load until a dialog handler answers it.
`transient_failure` counts attempts per path in the session cookie and returns 503 twice before
succeeding, which is the only thing in the fixture that fails and then recovers, so it is the only
thing that can prove a retry actually retried.

### Decisions logged

No new ADR this phase. The fixture is a test target rather than a design decision, and the one real
choice (Flask over an installed dependency) is recorded above and in `fixtures/legacy_bank/README.md`.
ARCHITECTURE.md decision 5 already covers why the fixture stays hostile.

### Caveats / not done

- The accessibility tree of this app is deliberately thin, and that has a consequence worth expecting
  rather than discovering in Phase 4. With no ARIA, no `<label for=>`, and no landmarks, most form
  inputs will have NO accessible name, so the rank 1 locator strategy (role plus accessible name) will
  frequently fail to resolve on the subaccount form. The ranked list will have to fall back to
  relational hints (the caption in the adjacent cell) and to ordinals. That is the intended outcome and
  the reason the ranked strategy exists, but do not be surprised when the Phase 4 rank distribution
  skews away from rank 1 on this screen. The correct response is better relational strategies, never a
  friendlier fixture.
- `not_found` returns HTTP 200, not 404. The builder made that call where the prompt was silent and I
  agree with it: "no such member" is a business outcome the caller needs, not a protocol failure, which
  is the distinction CLAUDE.md says is the most common design mistake in this problem. `permission_denied`
  is 403 as specified.
- `transient_failure` counts attempts per path in the signed session cookie. Clearing cookies or
  starting a fresh session resets the counter. That is fine for a fixture and keeps it stateless, but it
  means the mode is per session, not global.
- `slow_load` is a flat 6 second sleep, as specified. Replay must wait on a condition rather than race
  it; there is no jitter to tune against.
- The Flask secret key defaults to the literal `legacy-bank-fixture-not-secret`, overridable with
  `FIXTURE_SECRET`. It is a fixture value and not a credential, but the Phase 13 secret sweep will see
  it, so it is called out here now rather than being investigated later.
- `.playwright-mcp/` is written into the repo root by the Playwright MCP browser during verification. It
  is now gitignored rather than deleted, since Phases 3, 4, and 12 will produce it again.
- I removed `fixture_server.log` from the repo root. Background server logs belong in the scratchpad,
  and later phases should keep them there.
- The fixture server is currently RUNNING on port 5055, started by me from a clean shell to prove the
  startup command. Stop it with `Get-NetTCPConnection -LocalPort 5055 -State Listen | Stop-Process -Id
  { $_.OwningProcess } -Force`, or leave it up for Phase 2.
- Tenant B is untouched, as instructed. `fixtures/legacy_bank/` is tenant A only and Phase 12 adds the
  second tenant.

### Human-review items  (you confirm these)

- [ ] The fixture is hostile in a way that resembles software you have actually met — check: start it
  and click through `http://127.0.0.1:5055/login` in a browser — what you should see: a frameset, tiny
  serif text, tables with borders, and a form you cannot submit without JS. If it looks too tidy to be
  a real legacy app, say so now, because sanitizing it later is not allowed.
- [ ] The nine modes cover the runtime conditions you want replay judged on — check:
  `fixtures/legacy_bank/README.md` — what you should see: the nine modes listed with one line each.
- [ ] CI still green after you push — check: the Actions run. Nothing in this phase runs in CI (the
  fixture has no tests yet), so the only new risk is the `flask` dependency failing to install.

Phase 1 is complete and every machine gate is green. Say "proceed to Phase 2" when you have looked it
over.
