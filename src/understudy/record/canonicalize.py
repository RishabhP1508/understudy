"""record/canonicalize.py: route and value parameterization.

Turns the literal values a recorded run actually used into named, typed InputParams wherever the
GOAL TEXT ITSELF named that value -- "member 12345" in the goal makes "12345" a parameter, not a
frozen constant, so the recorded capability replays for any member, not only 12345. Two related
but separate transformations live here:

- Value parameterization (`goal_literals`, `infer_param_name`, `infer_type`): a step's own typed
  literal, if it matches a goal literal, becomes a named parameter -- record/recorder.py is what
  builds the actual `ParamRef`/`InputParam` pair, since only it has the full step and observed
  target context to draw the name from.
- Route canonicalization (`canonicalize_route`): the SAME literal, wherever it appears as a URL
  path segment or query value, is replaced with a route-style placeholder (`:name`, the
  Flask/Express convention) so a Checkpoint's recorded URL generalizes the same way the step that
  produced the value did.

KNOWN, ACCEPTED LIMIT: only a goal literal that is a standalone run of digits is recognized (an id
like "12345"). Every literal actually observed in this project's own recordings is numeric; a
smarter extractor (quoted strings, proper nouns) is future work, not attempted here. A typed value
with no digit-literal match and no pre-existing sensitivity redaction (record/recorder.py's other,
separate path for a "${param:...}" placeholder PolicyGate already produced) stays a hardcoded
literal -- e.g. this project's own "admin" login username, which the goal text never names.

Sensitivity inference reuses safety/redact.py's own named-pattern vocabulary
(`classify_field_sensitivity`) rather than a second, separately maintained list of PII/secret
field-name keywords.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

_GOAL_LITERAL_RE = re.compile(r"\d+")


def goal_literals(goal: str) -> set[str]:
    """Every standalone run of digits in the goal text -- the only literal shape this recorder
    looks for a parameter behind."""
    return set(_GOAL_LITERAL_RE.findall(goal))


def _snake_case(name: str) -> str:
    """A schema-identifier-style slug ("Member ID" -> "member_id"), distinct from
    safety.redact.slugify_param_name's dash-separated DISPLAY slug ("member-id"): this name
    becomes a Capability.inputs[].name and a route placeholder, both of which read better, and
    are more likely to be valid identifiers a calling agent can pass as a kwarg, in snake_case.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def infer_param_name(candidate_field_name: str, fallback: str) -> str:
    """Derive a parameter name from the UI field a value was typed into ("Member ID" ->
    "member_id"); falls back to a caller-supplied name only when there is no field name at all
    to derive one from."""
    slug = _snake_case(candidate_field_name)
    return slug or fallback


def infer_type(value: str) -> str:
    return "integer" if value.isdigit() else "string"


def canonicalize_route(url: str, value: str, param_name: str) -> str:
    """Replace `value` wherever it appears as a whole PATH SEGMENT or QUERY VALUE in `url` with
    the route-style placeholder `:param_name` (e.g. "/member/12345" -> "/member/:member_id").
    Never a substring match inside a longer segment -- every literal this recorder recognizes is
    a whole path segment or a whole query value, never part of one.

    Query values are rebuilt without `urlencode` deliberately: the recorded URL is a descriptive
    template for a reviewer, never dispatched as a real request, and `urlencode` would percent-
    encode the placeholder's own leading `:` (`%3A`), which is unreadable for no safety benefit --
    the original query value being replaced was itself a bare literal with nothing to escape.
    """
    split = urlsplit(url)
    segments = split.path.split("/")
    new_segments = [f":{param_name}" if segment == value else segment for segment in segments]
    new_path = "/".join(new_segments)

    if split.query:
        pairs = parse_qsl(split.query, keep_blank_values=True)
        new_query = "&".join(
            f"{key}={f':{param_name}' if val == value else val}" for key, val in pairs
        )
    else:
        new_query = split.query

    return urlunsplit((split.scheme, split.netloc, new_path, new_query, split.fragment))
