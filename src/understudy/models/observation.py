"""UIElement and Observation: the model's only view onto a live surface.

There is no HTML here, ever. See docs/adr/0002-accessibility-tree-over-screenshots.md for why
perception is built from the accessibility tree instead, and
docs/adr/0004-name-derivation-for-unlabeled-controls.md for how `name` and `name_source` are
derived when the application gives an element no accessible name of its own.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

# Bumped whenever perception itself changes shape (a new derivation rule, a new signal) in a way
# that can make a previously-recorded locator stop resolving even though the app did not change.
# `Provenance.perception_version` on an artifact records what this constant was AT RECORD TIME;
# replay/engine.py compares the two only to CLASSIFY a locator failure that already happened
# (stale_perception vs locator_unresolved, docs/adr/0009), never as a pre-flight gate -- a
# mismatch alone does not stop replay from attempting to resolve.
PERCEPTION_VERSION = 2

# Phase 12 (R7): a coarse "is this the same vendor product, same rendered version" signal for
# drift detection ACROSS TENANTS -- distinct from PERCEPTION_VERSION above, which tracks drift in
# OUR OWN reading of a page, and from a locator's own recorded_rank, which tracks drift in ONE
# element's resolution. Deliberately structural only, never text content: two tenants' own renamed
# labels must NOT change this hash (that is exactly what a TenantOverlay's vocabulary_map absorbs,
# models/artifact.py), but a genuinely different screen shape should.
_INTERACTIVE_ROLES = frozenset(
    {
        "textbox",
        "searchbox",
        "combobox",
        "button",
        "link",
        "checkbox",
        "radio",
        "option",
        "select",
    }
)


def app_fingerprint(observation: Observation) -> str:
    """A stable hash of the entry screen's STRUCTURAL signature: how many frames are loaded
    (`len(observation.urls)`, decision 64's own "every loaded frame" list), how many elements of
    each INTERACTIVE role are present, and the screen's own title/heading text. Pure and
    deterministic -- the same Observation hashes the same every time -- and a structurally
    different one (a different frame count, a different control mix, a different title) hashes
    differently.

    Captured once, from the FIRST observation after navigating to the target (agent/loop.py),
    carried into `TargetApp.app_fingerprint` by record/recorder.py, and recomputed at replay time
    (replay/engine.py) purely as a drift SIGNAL -- a mismatch warns, it never fails a replay and
    never gates one, the same non-gating stance PERCEPTION_VERSION already takes above.
    """
    role_counts: dict[str, int] = {}
    for element in observation.elements:
        if element.role in _INTERACTIVE_ROLES:
            role_counts[element.role] = role_counts.get(element.role, 0) + 1
    headings = sorted(
        element.name
        for element in observation.elements
        if element.role == "heading" and element.name
    )
    payload = json.dumps(
        {
            "frame_count": len(observation.urls),
            "role_counts": dict(sorted(role_counts.items())),
            "title": observation.title,
            "headings": headings,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Phase 8 (docs/adr/0012): the field-marking vocabulary safety/redact.py's Redactor consults so
# R3 (the whole-string credential-shaped-literal rule) applies only to a VALUE_CARRYING field --
# what was actually typed, an example value, an extracted output -- never to a STRUCTURAL one: an
# id, a role/name, an enum, a URL, a checkpoint's own target/value. Declared here, not in
# safety/redact.py itself, so every model module that marks a field (models/artifact.py,
# surface/base.py, surface/locator.py) can import it without a circular import back through
# redact.py, which already imports Observation/UIElement from this module.
FIELD_MARKING_KEY = "field_marking"
STRUCTURAL = "structural"
VALUE_CARRYING = "value_carrying"
# Pre-built, explicitly `dict[str, Any]`-typed `json_schema_extra` payloads: every module that
# marks a field (this one, models/artifact.py, surface/base.py, surface/locator.py) imports these
# rather than writing `{FIELD_MARKING_KEY: STRUCTURAL}` inline at each Field() call, because a
# freshly-written dict literal there infers as `dict[str, str]`, which mypy then rejects as
# incompatible with `Field`'s own `dict[str, Any]`-shaped parameter (dict is invariant).
STRUCTURAL_EXTRA: dict[str, Any] = {FIELD_MARKING_KEY: STRUCTURAL}
VALUE_CARRYING_EXTRA: dict[str, Any] = {FIELD_MARKING_KEY: VALUE_CARRYING}


class UIElement(BaseModel):
    node_id: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    role: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    name: str = Field(default="", json_schema_extra=STRUCTURAL_EXTRA)
    # Unmarked (falls back to VALUE_CARRYING, today's behaviour): a displayed value can genuinely
    # be, or contain, a real secret or PII value, unlike the structural fields above.
    value: str | None = None
    states: list[str] = Field(default_factory=list)
    # Bounds stay None unless something actually needs them for this element (surface/web.py's
    # fill_bounds is only ever called for elements already known to be sensitive), so perception
    # itself stays cheap -- see docs/adr/0008-field-sensitivity-redaction.md.
    bounds: list[float] | None = None
    # Data-driven, not inferred from prose (docs/adr/0008): "secret" for a structural password
    # field or a name/attribute matching policy.sensitive_fields.secret, "pii" for a match against
    # policy.sensitive_fields.pii, "none" otherwise. Populated during perception
    # (surface/web.py's _resolve_attr_names), never guessed from a rationale string. Structural
    # in the D4 (Phase 8) sense too: a closed vocabulary, not page content.
    sensitivity: Literal["none", "secret", "pii"] = Field(
        default="none", json_schema_extra=STRUCTURAL_EXTRA
    )
    # Nesting depth in the accessibility tree. This is the only thing that distinguishes two
    # nameless fields sitting in different table rows (see render() below), so it is carried
    # on the element rather than reconstructed from the raw tree text at render time.
    depth: int = 0
    # Frame segments from the root to this element, outermost first. Root-frame elements get
    # []. A node inside one iframe gets one segment, inside a nested iframe two, and so on.
    frame_path: list[str] = Field(default_factory=list)
    # (role, name) of the nearest ancestors, nearest first, capped at 5. The full chain to the
    # root is noise this app's prompt does not need; Phase 4's relational locator hints do.
    ancestors: list[tuple[str, str]] = Field(
        default_factory=list, json_schema_extra=STRUCTURAL_EXTRA
    )
    # Which strategy produced `name`, so a reader can tell an authored name from an inferred
    # one: "a11y" (the browser computed it), "row_label" / "column_header" (table structure),
    # "attr_name" (placeholder or name attribute), or "none" (no name found at all).
    name_source: str = Field(default="none", json_schema_extra=STRUCTURAL_EXTRA)


class Observation(BaseModel):
    url: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    title: str
    elements: list[UIElement]
    # Every URL currently loaded (the shell plus every child frame), mirroring `Surface.urls()`
    # -- populated by WebSurface.observe() (Phase 8, D3). `checkpoint_satisfied`'s `url_matches`
    # kind checks this list, not `url` alone: on a frameset app the top-level URL is frequently
    # constant across every screen (docs/adr/0005), so a page-level check would pass on the wrong
    # screen. Defaults to [] so every existing bare Observation() construction in the test suite
    # keeps working unmodified; a fake surface that never populates it simply never satisfies a
    # url_matches checkpoint, which is the correct, safe default (never a false match).
    urls: list[str] = Field(default_factory=list, json_schema_extra=STRUCTURAL_EXTRA)

    def digest(self) -> str:
        """A stable hash of the STRUCTURAL signature: (role, name, name_source, frame_path,
        depth) across every element. Deliberately excludes `value`, so a page whose text
        content changed but whose structure did not still hashes the same. Phase 7's
        no-progress detection depends on that distinction.
        """
        payload = json.dumps(
            [
                [element.role, element.name, element.name_source, element.frame_path, element.depth]
                for element in self.elements
            ],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def render(self, max_elements: int = 200) -> str:
        """Compact indexed text, never HTML.

        [index] is how a tool call addresses an element; the surface maps that index back to a
        live ref at act() time. Indentation preserves adjacency, which is the only way to tell
        two same-named or unnamed fields apart on a form with no <label>. `max_elements` caps
        how much of a large page is shown; when the cap truncates, a final line says how many
        elements were left out, so the model is never silently shown a partial page.
        """
        lines = [f"URL: {self.url}"]
        shown = self.elements[:max_elements]
        for index, element in enumerate(shown):
            indent = "  " * element.depth
            piece = element.role
            if element.name:
                piece += f' "{element.name}"'
            line = f"[{index}] {indent}{piece}"
            if element.value:
                line += f": {element.value}"
            lines.append(line)
        omitted = len(self.elements) - len(shown)
        if omitted > 0:
            lines.append(
                f"... {omitted} more element(s) omitted (showing first {max_elements} of "
                f"{len(self.elements)})"
            )
        return "\n".join(lines)
