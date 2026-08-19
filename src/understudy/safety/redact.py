"""Redactor: the single serialization path. Nothing reaches disk except through dumps() (or,
for screenshots, redact_screenshot()) -- ARCHITECTURE.md decision 10.

Phase 2's rule was a single whole-string keyword check: any string containing a credential-shaped
substring (including the word "password") was replaced entirely. That is why a rationale reading
"Enter the password to log in" and a rationale quoting a real secret both became "[REDACTED]": the
rule could not tell prose that MENTIONS a field from a literal that IS a value. This rewrite fixes
both directions at once, and neither fix is a special case for a particular sentinel value:

  R0. A parameter reference (a bare "${...}" string) is returned unchanged -- it is a
      placeholder, not a value.
  R1. Values registered via register_secret are redacted by VALUE, wherever the substring
      occurs, including inside prose. This is how a rationale that quotes a real secret gets
      caught -- never by scanning for the word "password".
  R2. Named PII patterns (ssn, card number, account number, dob, email, phone) redact only the
      matched span, not the whole string, so prose survives around a value it happens to contain.
  R3. A CREDENTIAL-SHAPED LITERAL -- a string with no whitespace, NOT purely alphabetic, that
      contains a credential token (secret, passwd, password, token, apikey, api_key,
      private_key), case-insensitively -- is redacted whole. "SECRET_SENTINEL_VALUE" has
      underscores, so it is still caught. "Enter the password to log in" has whitespace, so it
      survives. "Password" and "password" are plain alphabetic words, so a caption survives too,
      at any nesting depth, because this check is about the VALUE'S OWN SHAPE, never which key or
      how deep in the tree it sits.

      The all-alphabetic exclusion exists because this app's own password field is named
      "Password" by the row-label rule (docs/adr/0004): an earlier version of this rule keyed the
      exemption on WHICH DICT KEY a string came from (`name`, `role`, ...), and that broke the
      moment the same caption appeared inside a LIST one level deeper -- `TargetDescriptor.scope`
      is a list of (role, name) ancestor pairs, so "Password" inside `scope` was redacted while
      the identical caption in the top-level `name` field survived, purely because list items
      carry no key. A position-dependent rule is exactly the kind of bug this rule exists to
      prevent, one level deeper. Shape, not position, is what a string's OWN value can guarantee
      about itself regardless of where it is serialized. R1 and R2 still apply everywhere,
      including to all-alphabetic strings: a genuine secret or PII value that happens to be
      alphabetic-only is still caught by those once registered or matched. The remaining, accepted
      limit: a credential-shaped literal that is ALSO purely alphabetic (no digits, no symbols --
      a real secret essentially never has this shape) is not caught by R3 alone (docs/adr/0008).

`_redact_model_instance`/`_redact_value` additionally honour field-level sensitivity carried IN
the data: a dict containing `"sensitivity": "secret"` has its `value`/`text` keys replaced with a
parameter reference; `"sensitivity": "pii"` has them masked. This is what makes a serialized
element safe
with no keyword rule at all -- see docs/adr/0008.

Phase 8 (D4, docs/adr/0012) adds a second, independent mechanism on top of the above: R3 (the
whole-string credential-shaped-literal rule) is applied only to a field a schema author marked
VALUE_CARRYING (`models/observation.FIELD_MARKING_KEY`); a field marked STRUCTURAL (an id, a
role/name, an enum, a URL, a checkpoint's own target/value) is exempt from it, because R3's
keyword match ("token" inside "DONE_TOKEN", "secret" inside "/secret-flow") does not know it is
looking at a fixed identifier rather than a value, and destroyed both in earlier phases. R1
(registered secret values) and R2 (named PII patterns) still apply to EVERY field regardless of
marking: a real secret or PII value landing in a structural field must still be caught. This
walks the ACTUAL pydantic model tree (not a pre-dumped plain dict), because that is the only way
to know which field a given string came from; the moment the walk reaches an untyped `dict[str,
Any]` (a `RunEvent`'s own `context`/`proposed_action`/`policy_decision`/... fields, or a bare dict
with no model behind it at all, e.g. tests/test_constraints.py's own sentinel payload), there is
no schema left to consult, so every string beneath that point falls back to VALUE_CARRYING --
today's behaviour, unchanged. The residual limit: marking is only as good as the schema author's
classification, and this fallback means a value the schema happens to keep in an untyped dict
gets no field-level exemption at all (R3 still applies to it, same as before Phase 8).
"""

from __future__ import annotations

import io
import json
import re
import uuid
from typing import Any, Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from understudy.models.observation import FIELD_MARKING_KEY, STRUCTURAL, Observation, UIElement

_REDACTED = "[REDACTED]"

_PARAM_REF_RE = re.compile(r"^\$\{[^{}]*\}$")

_CREDENTIAL_TOKENS = (
    "secret",
    "passwd",
    "password",
    "token",
    "apikey",
    "api_key",
    "private_key",
)

# Named PII patterns. Order matters: card_number is applied before account_number so a 13-19
# digit card number is redacted as a card number first, leaving nothing for the shorter,
# broader account_number pattern to also see (it would otherwise be redundant, not wrong, but
# the ordering is what the phase spec requires and it is cheap to honour exactly).
_PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "card_number": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    # \b\d{9,}\b: a run of 9+ ASCII digits with a word boundary on both sides. A 64-char hex
    # transcript_hash never matches this -- not because it lacks a boundary (it does, at its own
    # start and end), but because a sha256 hex digest mixes digits and a-f letters throughout, so
    # it never contains 9 CONSECUTIVE digit characters for \d{9,} to find (verified against the
    # real digest in artifacts/, see tests/test_phase5.py).
    "account_number": re.compile(r"\b\d{9,}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # Slash form only. provenance.created_at is an ISO timestamp
    # ("2026-08-15T00:50:46.099808+00:00"); \d{4}-\d{2}-\d{2} would eat it, so dob is
    # deliberately never matched against dash-separated digits, only the "MM/DD/YYYY" shape a
    # human actually types into a form field.
    "dob": re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}\b"),
    # Separators are mandatory (not optional) between groups, deliberately: an optional, possibly
    # absent separator would let this pattern match a run of digits that merely happens to fall
    # into 3-3-4 groups with nothing between them (an ISO timestamp's fractional seconds, for
    # instance), which account_number/card_number already cover if it is genuinely one long run.
    "phone": re.compile(r"\b(?:\+?1[-. ])?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b"),
}


def mint_safe_id(prefix: str = "id", length: int = 12) -> str:
    """Mint a short opaque id (a run id, an intervention id, ...) that is GUARANTEED to survive
    `Redactor().dumps()` unchanged, for every id this codebase writes to disk and later looks up
    by value (`EvidenceLogger`'s `run_id`, `Capability.provenance.run_id`, an
    `InterventionRequest.id` looked up again in the store).

    The hazard this closes: a bare `uuid.uuid4().hex[:n]` slice is all-ASCII-digits with
    probability 0.625**n (10 of 16 hex digits are decimal digits), which is NOT negligible at the
    lengths this codebase actually uses -- about 1 in 110 at n=10, about 1 in 281 at n=12. R2's
    `account_number` pattern (`\\b\\d{9,}\\b`) applies to EVERY field unconditionally, regardless
    of STRUCTURAL marking (ARCHITECTURE.md decision 65, docs/adr/0008/0012) -- there is no
    per-field exemption to add here, on purpose. So an all-digit id is silently rewritten to
    "[REDACTED]" the first time it is serialized, which severs it from every later lookup by that
    same value: a run's own `run_id` no longer names the evidence directory it came from, an
    intervention id no longer resolves in the store.

    A fixed, non-digit, no-separator PREFIX is the fix: it keeps the id one contiguous `\\w` token
    with no internal word boundary for `\\b\\d{9,}\\b` to anchor on, so no substring of the result
    can ever match, regardless of what the random hex tail turns out to be -- proven, not just
    argued, by test_phase5.py's iterated redaction test.
    """
    return f"{prefix}{uuid.uuid4().hex[:length]}"


def slugify_param_name(name: str) -> str:
    """A short, stable slug for a `${param:<slug>}` reference. Shared by PolicyGate (which builds
    the reference for a Type action's own text) and _redact_value's field-level mechanism below,
    so the two never drift into two different slugging rules."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "secret"


def classify_field_sensitivity(field_name: str) -> Literal["none", "secret", "pii"]:
    """Sensitivity of a DERIVED field/parameter name (record/canonicalize.py), reusing this
    module's own two named-pattern tables instead of a second, separately maintained list of
    PII/secret field-name keywords: `_CREDENTIAL_TOKENS` (R3's vocabulary) for "secret",
    `_PII_PATTERNS`'s own key names (ssn, card_number, account_number, dob, email, phone) for
    "pii", matched as a case-insensitive substring of the field name itself.
    """
    lowered = field_name.lower()
    if any(token in lowered for token in _CREDENTIAL_TOKENS):
        return "secret"
    if any(key in lowered for key in _PII_PATTERNS):
        return "pii"
    return "none"


def _is_param_reference(value: str) -> bool:
    return bool(_PARAM_REF_RE.fullmatch(value))


def _is_credential_shaped_literal(value: str) -> bool:
    if any(ch.isspace() for ch in value):
        return False
    if value.isalpha():  # a plain word ("Password", "secret") is a caption, not a literal
        return False
    lowered = value.lower()
    return any(token in lowered for token in _CREDENTIAL_TOKENS)


def is_sensitive_element(element: UIElement) -> bool:
    """Public predicate: does this element carry a secret or PII value? EvidenceLogger uses this
    to decide which elements are worth the extra round trip of resolving live bounds for
    (surface/web.py's fill_bounds is never called for a whole observation, only for these)."""
    if element.sensitivity in ("secret", "pii"):
        return True
    for text in (element.name, element.value):
        if text and any(pattern.search(text) for pattern in _PII_PATTERNS.values()):
            return True
    return False


class Redactor:
    def __init__(self) -> None:
        self._registered_values: set[str] = set()

    def register_secret(self, value: str) -> None:
        """Register a literal value -- a secret or PII, the caller's job is done once it calls
        this, not the registry's -- so that wherever this exact substring appears in anything
        passed to dumps() afterwards, including inside a rationale, it is replaced (R1). The
        secret-vs-PII distinction (a parameter reference vs a `[REDACTED]` mask) lives at the
        call site (PolicyGate._log), not here: both end up redacted identically once registered.
        """
        if value:
            self._registered_values.add(value)

    def redact_text(self, value: str, *, value_carrying: bool = True) -> str:
        """`value_carrying=True` (the default) is today's behaviour: every rule applies, which is
        what every caller that hands this a raw string with no schema behind it -- a DOM
        snapshot, a bare dict with no model, capture_failure's HTML -- still gets (D4's residual
        limit). A model-tree walk (`_redact_any` below) passes `value_carrying=False` for a field
        a schema author marked STRUCTURAL, which skips R3 only; R0-R2 are unconditional.
        """
        if _is_param_reference(value):
            return value  # R0: a placeholder, not a value
        text = value
        for registered in self._registered_values:  # R1 -- every field, regardless of marking
            if registered and registered in text:
                text = text.replace(registered, _REDACTED)
        for pattern in _PII_PATTERNS.values():  # R2 -- every field, regardless of marking
            text = pattern.sub(_REDACTED, text)
        if value_carrying and _is_credential_shaped_literal(text):  # R3 -- VALUE_CARRYING only
            return _REDACTED
        return text

    def _sensitivity_marked_dict(self, value: dict[str, Any]) -> dict[str, Any] | None:
        """The pre-Phase-8 field-level mechanism, unchanged: a dict carrying its own
        `"sensitivity": "secret"/"pii"` key has its `value`/`text` keys replaced outright
        (a parameter reference, or a full mask), independent of R3 and of D4's marking -- this is
        how a serialized `UIElement`/similar stays safe with no keyword rule at all (docs/adr/0008).
        Returns None (nothing to do here) when `value` carries no such key, so the caller falls
        through to ordinary per-key recursion.
        """
        sensitivity = value.get("sensitivity")
        if sensitivity == "secret":
            slug = slugify_param_name(str(value.get("name") or "secret"))
            replacement: Any = f"${{param:{slug}}}"
        elif sensitivity == "pii":
            replacement = _REDACTED
        else:
            return None
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key in ("value", "text") and isinstance(item, str):
                redacted[key] = replacement
            else:
                redacted[key] = self._redact_any(item, value_carrying=True)
        return redacted

    def _redact_any(self, value: Any, *, value_carrying: bool) -> Any:
        """Redact an already-DUMPED Python value (a plain str/dict/list/tuple -- never a live
        BaseModel; see `_redact_field` for why). Used both for a plain, schema-less structure
        (where `value_carrying` is always True -- D4's residual limit) and for anything beneath a
        field whose own marking has already been decided by the caller.
        """
        if isinstance(value, str):
            return self.redact_text(value, value_carrying=value_carrying)
        if isinstance(value, dict):
            sensitivity_result = self._sensitivity_marked_dict(value)
            if sensitivity_result is not None:
                return sensitivity_result
            return {k: self._redact_any(v, value_carrying=value_carrying) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_any(item, value_carrying=value_carrying) for item in value]
        return value

    def _redact_field(self, live: Any, dumped: Any, *, value_carrying: bool) -> Any:
        """`dumped` is this field's own value from `obj.model_dump(mode="json")` -- already
        reflecting any `field_serializer`, alias, or JSON-mode conversion pydantic itself applies
        (e.g. TargetDescriptor.confidence's rounding, D6), so it is what actually gets redacted
        and returned. `live` is the SAME field's raw attribute, consulted ONLY to detect a nested
        BaseModel (or a list of them) worth recursing into for ITS OWN field markings -- `live`'s
        VALUES are never read directly, only its shape.
        """
        if isinstance(live, BaseModel):
            return self._redact_model_instance(live)
        if isinstance(live, (list, tuple)) and isinstance(dumped, list):
            return [
                self._redact_field(lv, dv, value_carrying=value_carrying)
                if isinstance(lv, BaseModel)
                else self._redact_any(dv, value_carrying=value_carrying)
                for lv, dv in zip(live, dumped, strict=True)
            ]
        return self._redact_any(dumped, value_carrying=value_carrying)

    @staticmethod
    def _field_marking(field: FieldInfo) -> str | None:
        extra = field.json_schema_extra
        if isinstance(extra, dict):
            marking = extra.get(FIELD_MARKING_KEY)
            if isinstance(marking, str):
                return marking
        return None

    def _redact_model_instance(self, obj: BaseModel) -> dict[str, Any]:
        """Walk `obj`'s OWN declared fields (never a pre-dumped plain dict, which would already
        have lost this type information) for D4's field markings, while reading actual VALUES
        from `obj.model_dump(mode="json")` so no pydantic-level serialization is bypassed. Extra
        fields (`model_config = {"extra": "allow"}`, e.g. RunEvent's ad hoc kwargs) have no
        declared FieldInfo and therefore no marking at all -- D4's residual limit, unchanged
        behaviour from before this phase.
        """
        dumped = obj.model_dump(mode="json")
        result: dict[str, Any] = {}
        for name, field in type(obj).model_fields.items():
            marking = self._field_marking(field)
            value_carrying = marking != STRUCTURAL
            result[name] = self._redact_field(
                getattr(obj, name), dumped.get(name), value_carrying=value_carrying
            )
        extra = getattr(obj, "model_extra", None) or {}
        for name in extra:
            result[name] = self._redact_any(dumped.get(name), value_carrying=True)
        return result

    def dumps(self, obj: Any, *, indent: int | None = None) -> str:
        if isinstance(obj, BaseModel):
            payload: Any = self._redact_model_instance(obj)
        else:
            payload = self._redact_any(obj, value_carrying=True)
        return json.dumps(payload, indent=indent)


def redact_screenshot(png_bytes: bytes, observation: Observation) -> bytes | None:
    """Draw an opaque box over every sensitive element's bounds. If a sensitive element has no
    resolved bounds, this returns None rather than writing a partially-masked image: fail safe,
    and let the caller log why the screenshot was skipped instead of shipping a leak.
    """
    sensitive = [element for element in observation.elements if is_sensitive_element(element)]
    if not sensitive:
        return png_bytes
    for element in sensitive:
        if element.bounds is None:
            return None

    image = Image.open(io.BytesIO(png_bytes))
    draw = ImageDraw.Draw(image)
    for element in sensitive:
        assert element.bounds is not None
        x, y, width, height = element.bounds
        draw.rectangle([x, y, x + width, y + height], fill="black")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
