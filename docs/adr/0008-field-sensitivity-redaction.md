# 0008. Redaction is driven by the field a value targets, never by keyword matching on prose

Status: accepted (Phase 5)

## Context

Phase 2 shipped a single rule: any string containing a credential-shaped substring, anywhere,
redacted the whole string. Verified live, this rule was wrong in both directions on the same run.

**Under-redaction.** A rationale that happened to quote a real secret value would only be caught if
the VALUE ITSELF looked credential-shaped (no whitespace, containing a token like "secret" or
"token"). A rationale that quoted a secret in the middle of an ordinary sentence ("the field now
holds hunter2") was prose, had whitespace, and the old rule never even looked at it, because it only
ever redacted a string as a whole, keyed on a keyword, never by matching an actual value.

**Over-redaction.** A rationale that merely MENTIONED a sensitive field name kept getting nuked
entirely. "Enter the password to log in" became "[REDACTED]" in both `run.jsonl` and the recorded
artifact, because "password" is a substring of that sentence and the whole-string rule does not
distinguish a sentence that mentions a field from a literal that is a value. This destroys the R5
"why" on exactly the step where it matters most.

## Decision

Sensitivity is data-driven: `UIElement.sensitivity` (`"none" | "secret" | "pii"`) is set during
perception, never inferred from a rationale string. `surface/web.py._resolve_attr_names` sets it from
two signals, in order: `type === "password"` (structural, the strongest signal there is), then a
case-insensitive substring match of the element's name/name-attribute/id/autocomplete against
`policy.sensitive_fields.secret` / `.pii`. `PolicyGate.dispatch` reads this field, not the rationale,
to decide what to redact.

`Redactor` fixes both directions with four rules, none a special case for a particular sentinel:

- **R0** A parameter reference (`"${...}"`) is returned unchanged — it is a placeholder, not a value.
- **R1** Registered secret values (`register_secret`) are redacted by VALUE, as a substring,
  wherever they occur, including inside prose. This is what catches a rationale that quotes a real
  secret — by value, never by scanning for the word "password". `PolicyGate` registers a Type
  action's actual text with the logger's redactor before logging it, so the same value is caught
  wherever it later reappears. The secret-vs-PII distinction (a `${param:...}` reference vs a
  `[REDACTED]` mask) is made once, at that call site, not in the registry — both are redacted
  identically once registered, so one method does the registering.
- **R2** Named PII patterns (ssn, card number, account number, dob, email, phone) redact only the
  matched span, not the whole string, so prose survives around a value it happens to contain.
- **R3** A credential-shaped literal — a string with NO whitespace, NOT purely alphabetic,
  containing a credential token (secret, passwd, password, token, apikey, api_key, private_key),
  case-insensitively — is redacted whole. "SECRET_SENTINEL_VALUE" has underscores, so it is still
  caught. "Enter the password to log in" has whitespace, so it survives. "Password" and "password"
  are plain alphabetic words, so a caption survives too. The whitespace test is the line between a
  literal value and a sentence that mentions one; the all-alphabetic test is the line between a
  literal value and a caption copied verbatim from the target app's own UI (see below) — together
  they are what stops both directions of the Phase 2 bug from recurring.

`redact_model`/`_redact_value` additionally honour field-level sensitivity carried in the data itself:
a dict with `"sensitivity": "secret"` has its own `value`/`text` keys replaced by a `${param:<slug>}`
reference; `"sensitivity": "pii"` has them masked. This is what makes a serialized `UIElement` safe
with no keyword rule at all — the redaction is driven by what the field IS, not by what its value
happens to say.

### A second over-redaction found while building this fix, and why it is a SHAPE rule, not a key rule

R3, applied uniformly to every string in the tree, has the same shape problem in a different place:
this fixture's own password field is named "Password" by the row-label rule (`docs/adr/0004`), and
"Password" is itself a bare, no-whitespace string containing the token "password". Verified directly
(`tests/test_phase5.py`, before this fix): serializing that field's recorded `TargetDescriptor`
replaced `"name": "Password"` with `"name": "[REDACTED]"`. A caption is not a secret; redacting it
protects nothing while making the artifact harder to read, and in the worst case — a role-filtered
pool of exactly one candidate, where `surface/locator.py`'s ordinal-rescue rule refuses to rescue a
descriptor that once carried a meaningful name — corrupts the descriptor enough to make it
unresolvable at replay.

The first fix for this exempted a fixed set of dict keys (`role`, `name`, `name_source`, ...) from
R3. That fix was itself broken, and broken in exactly the position-dependent way this whole ADR is
about: `_redact_value` only knew a string's dict key when the string was a DIRECT child of a dict.
`TargetDescriptor.scope` is a list of `(role, name)` ancestor pairs, so the same caption "Password"
sitting one level deeper inside that list carried no key at all and got redacted anyway, while the
identical caption at the top-level `name` field survived — the exact bug this ADR set out to fix,
recurring one level down, and worse there: `scope` feeds `ROLE_NAME_SCOPED` and narrows
`_role_pool`, so a corrupted scope entry silently degrades both the scoped strategy and the ordinal
pool at replay. The same hit applies to any list of plain strings — a serialized policy's own
`sensitive_fields.secret` list, for instance, would come back all-`[REDACTED]`.

The fix is a SHAPE rule instead of a key rule: R3 additionally requires the string not be purely
alphabetic (`not value.isalpha()`). "Password" and "secret" (the sensitivity marker's own value) are
plain words and now survive at ANY nesting depth, in a dict or a list, with no key lookup involved at
all. "SECRET_SENTINEL_VALUE" still has underscores, so it is still caught, and invariant 3 still
holds. This is a smaller, cheaper rule than the key exemption it replaces, and it cannot go stale as
the schema grows a new key or a new nesting level, because it never asked "which key is this" in the
first place. R1 and R2 are unaffected and still apply everywhere, including to alphabetic strings: a
genuine secret or PII value that happens to be alphabetic-only is still caught once registered or
matched by a named pattern.

### Lazy bounds resolution

`UIElement.bounds` stays `None` for almost every element. `WebSurface.fill_bounds` resolves a live
bounding box only for elements a caller has already determined are sensitive
(`safety.redact.is_sensitive_element`), never for a whole observation. Perception therefore pays for a
`bounding_box()` round trip exactly where it is needed and nowhere else — the same "only what is
used" discipline `_resolve_attr_names` already applies to attribute reads.

### Pillow

`redact_screenshot` decodes the PNG, draws opaque boxes, and re-encodes, using Pillow. The stdlib
alternative is a hand-rolled PNG codec: chunk parsing, zlib inflate/deflate, per-scanline un-filtering
across five filter types, re-filtering, and a CRC — on the order of a hundred lines of bit
manipulation, inside the safety module, where a bug puts the mask in the wrong place. Pillow reduces
this to opening the image, drawing a filled rectangle per sensitive element, and saving — about ten
lines, and it ships its own type stubs (`py.typed`), so no separate stub package is needed.

## Remaining limits, stated plainly

- **Value-scanning does not catch paraphrase.** R1 redacts a registered value by exact substring
  match. A model that repeats the secret verbatim in a rationale is caught; a model that paraphrases
  it (spells it differently, describes it instead of quoting it) is not. There is no general defence
  against a paraphrased secret without either much more expensive matching or refusing to log
  free-text rationale at all, and the latter throws away the R5 "why" this whole design exists to keep.
- **A secret is only redacted from events written AFTER it is first registered.** `register_secret`
  is called at the gate, at the moment a Type action into a sensitivity="secret" field is dispatched.
  Anything already written to `run.jsonl` before that point (there should be nothing — the field's
  sensitivity is known from perception before the action runs — but this is a structural property of
  the implementation, not a guarantee enforced elsewhere) would not be retroactively redacted.
- **R3 does not catch an all-alphabetic credential literal.** A secret whose entire value happens to
  be letters only (no digit, no underscore, no symbol -- a real generated credential essentially
  never has this shape, but a weak hand-typed one could) is indistinguishable, by shape alone, from
  an ordinary caption, and R3 lets it through. R1 remains the precise backstop: once such a value is
  registered, it is still redacted by exact substring match wherever it recurs.
- **R3 still over-redacts an ordinary identifier that contains a credential word.** This is the
  converse of the limit above and it is the accepted cost of keeping R3 at all. Measured on a real
  run of the pipeline: a checkpoint value `DONE_TOKEN` and a URL path `/secret-flow` are both
  redacted whole, because each is whitespace-free, non-alphabetic, and contains a credential token.
  Neither is a secret. The two strings are structurally indistinguishable from
  `SECRET_SENTINEL_VALUE`, so no shape rule can separate them, and the key-based rule that could
  was removed above for being position-dependent. The real fixture is unaffected (no route or
  checkpoint value in `policies/legacy_bank.yaml` or the recorded artifact contains a credential
  word), and the failure mode is loud rather than silent: a blanked checkpoint value cannot match,
  so replay returns a `HardFailure` naming it rather than passing wrongly. The principled fix is to
  mark value-carrying versus structural fields on the schema itself and apply R3 only to the
  former; that belongs with the artifact schema work in Phase 8, not to a heuristic patch here.
