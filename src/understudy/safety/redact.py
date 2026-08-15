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

`redact_model`/`_redact_value` additionally honour field-level sensitivity carried IN the data:
a dict containing `"sensitivity": "secret"` has its `value`/`text` keys replaced with a parameter
reference; `"sensitivity": "pii"` has them masked. This is what makes a serialized element safe
with no keyword rule at all -- see docs/adr/0008.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

from PIL import Image, ImageDraw
from pydantic import BaseModel

from understudy.models.observation import Observation, UIElement

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


def slugify_param_name(name: str) -> str:
    """A short, stable slug for a `${param:<slug>}` reference. Shared by PolicyGate (which builds
    the reference for a Type action's own text) and _redact_value's field-level mechanism below,
    so the two never drift into two different slugging rules."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "secret"


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

    def redact_text(self, value: str) -> str:
        if _is_param_reference(value):
            return value  # R0: a placeholder, not a value
        text = value
        for registered in self._registered_values:  # R1
            if registered and registered in text:
                text = text.replace(registered, _REDACTED)
        for pattern in _PII_PATTERNS.values():  # R2
            text = pattern.sub(_REDACTED, text)
        if _is_credential_shaped_literal(text):  # R3
            return _REDACTED
        return text

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return self._redact_dict(value)
        if isinstance(value, (list, tuple)):
            return [self._redact_value(item) for item in value]
        return value

    def _redact_dict(self, value: dict[str, Any]) -> dict[str, Any]:
        sensitivity = value.get("sensitivity")
        if sensitivity == "secret":
            slug = slugify_param_name(str(value.get("name") or "secret"))
            replacement: Any = f"${{param:{slug}}}"
        elif sensitivity == "pii":
            replacement = _REDACTED
        else:
            replacement = None

        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if replacement is not None and key in ("value", "text") and isinstance(item, str):
                redacted[key] = replacement
            else:
                redacted[key] = self._redact_value(item)
        return redacted

    def redact_model(self, obj: BaseModel) -> dict[str, Any]:
        redacted: dict[str, Any] = self._redact_value(obj.model_dump(mode="json"))
        return redacted

    def dumps(self, obj: Any, *, indent: int | None = None) -> str:
        payload = self.redact_model(obj) if isinstance(obj, BaseModel) else self._redact_value(obj)
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
