# Legacy Bank fixture

This is a test target, not evaluated code. It exists so the discovery agent has a
real, hostile UI to drive, and so replay has real failure conditions to detect. We
built it instead of pointing the agent at a public demo banking site for three
reasons: a public site cannot be made to fail on demand, so there is no way to
exercise validation errors, permission denials, or timeouts; it is not ours to
script and hammer with an automated agent; and a target outside our control means
the same run is not reproducible from one day to the next.

The app is deliberately ugly. No CSS framework, no ARIA, no test IDs, no `id`
attributes used as hooks, no `<label for=>`. Class names and form field names are
meaningless (`td3`, `f1`, `f7`). Submit controls are `<input type="button">` with
an inline `onclick` that calls `this.form.submit()`, not a real submit input, so a
script that only follows links or reads a DOM tree without a JS-capable browser
will not get past a single form. The point is to force the locator strategy to
work the way it would have to against a real 20-year-old back office screen.

## Screens

- `/login` - username and password, any non-empty pair is accepted, sets a session.
- `/app` - the outer shell: a real HTML frameset with no `<body>` tag, nav frame
  and content frame.
- `/nav` - the nav frame's contents.
- `/members` - search by member ID.
- `/member/<id>` - name, status, and savings balance; the balance itself renders
  inside a nested `<iframe>` fetched from `/member/<id>/balance`.
- `/member/<id>/subaccount/new` - opens a subaccount: account type, nickname,
  initial deposit. All three fields are labeled only by text in the adjacent table
  cell, never a real `<label>`.
- `/member/<id>/subaccount/confirm` - shows the reference number for the new
  subaccount.
- `/external` - redirects off-site, to `https://example.com/`, so the navigation
  allowlist has something real to refuse.

## Injection modes

Failures are picked two ways: `?inject=<mode>` on any request, for one request
only, or `POST`/`GET /admin/inject?mode=<mode>`, which persists the mode in the
session until you send `mode=none` (or `mode=clear`). `/login` and `/admin/inject`
are exempt from injection so a stuck mode can never lock you out.

- `validation` - the subaccount form re-renders with a field error next to the
  deposit box, HTTP 200.
- `not_found` - a "no such member" result page.
- `permission_denied` - a permission error page, HTTP 403.
- `unexpected_dialog` - an HTML interstitial instead of the target screen, with a
  dismiss link back to the same path plus `?dismiss=1`.
- `native_dialog` - a real `window.confirm()` fired via an injected `<script>`,
  a different mechanism from `unexpected_dialog` on purpose.
- `session_expired` - clears the session and redirects to `/login`.
- `slow_load` - a flat six second delay before the page renders.
- `transient_failure` - HTTP 503 on the first two requests to a path in this
  session, then success on the third.
- `app_error` - HTTP 500 with a generic error page.

## Seed members

Five synthetic IDs, none of them resembling a real person or a real SSN:

- `12345` - Testuser Alpha, active, the happy path.
- `22222` - Sample Bravo, active.
- `33333` - Fixture Charlie, inactive.
- `55555` - permission-restricted; its detail page always returns the permission
  error, with no injection needed.
- `99999` - does not exist; its detail page always returns the not-found page,
  with no injection needed.
