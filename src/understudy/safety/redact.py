"""Redactor: the single serialization path. Nothing reaches disk except through dumps().

Two general rules, neither a special case for any particular sentinel value (that would be
gaming the check tests/test_constraints.py runs against SECRET_SENTINEL_VALUE and
123-45-6789): a string containing an SSN-shaped substring, and a string containing a
credential-token substring, case-insensitively. Either match redacts the WHOLE string, not just
the matched part. Phase 5 replaces this with the full policy-driven redactor.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

# ponytail: known over-redaction, by design of the coarse whole-string rule below. Observed
# live: the model's own rationale "Enter the password to log in" became "[REDACTED]" in both
# run.jsonl and the artifact, because "password" matches _CREDENTIAL_TOKENS and the rule redacts
# the whole string, not just a literal secret value. That is the rule failing safe, but it
# destroys the R5 "why" on that step. Phase 5 should scope value-redaction to fields that carry
# literal values (typed args, extracted text) and treat free-text rationale as prose subject
# only to the SSN rule and to literal known-secret values, not to bare credential-token words.

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CREDENTIAL_TOKENS = (
    "secret",
    "passwd",
    "password",
    "token",
    "apikey",
    "api_key",
    "private_key",
)
_REDACTED = "[REDACTED]"


class Redactor:
    def redact_text(self, value: str) -> str:
        if _SSN_RE.search(value):
            return _REDACTED
        lowered = value.lower()
        if any(token in lowered for token in _CREDENTIAL_TOKENS):
            return _REDACTED
        return value

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {key: self._redact_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_value(item) for item in value]
        return value

    def redact_model(self, obj: BaseModel) -> dict[str, Any]:
        redacted: dict[str, Any] = self._redact_value(obj.model_dump(mode="json"))
        return redacted

    def dumps(self, obj: Any, *, indent: int | None = None) -> str:
        payload = self.redact_model(obj) if isinstance(obj, BaseModel) else self._redact_value(obj)
        return json.dumps(payload, indent=indent)
