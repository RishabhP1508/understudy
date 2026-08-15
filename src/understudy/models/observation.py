"""UIElement and Observation: the model's only view onto a live surface.

There is no HTML here, ever. See docs/adr/0002-accessibility-tree-over-screenshots.md for why
perception is built from the accessibility tree instead, and
docs/adr/0004-name-derivation-for-unlabeled-controls.md for how `name` and `name_source` are
derived when the application gives an element no accessible name of its own.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

# Bumped whenever perception itself changes shape (a new derivation rule, a new signal) in a way
# that can make a previously-recorded locator stop resolving even though the app did not change.
# `Provenance.perception_version` on an artifact records what this constant was AT RECORD TIME;
# replay/engine.py compares the two only to CLASSIFY a locator failure that already happened
# (stale_perception vs locator_unresolved, docs/adr/0009), never as a pre-flight gate -- a
# mismatch alone does not stop replay from attempting to resolve.
PERCEPTION_VERSION = 2


class UIElement(BaseModel):
    node_id: str
    role: str
    name: str = ""
    value: str | None = None
    states: list[str] = Field(default_factory=list)
    # Bounds stay None unless something actually needs them for this element (surface/web.py's
    # fill_bounds is only ever called for elements already known to be sensitive), so perception
    # itself stays cheap -- see docs/adr/0008-field-sensitivity-redaction.md.
    bounds: list[float] | None = None
    # Data-driven, not inferred from prose (docs/adr/0008): "secret" for a structural password
    # field or a name/attribute matching policy.sensitive_fields.secret, "pii" for a match against
    # policy.sensitive_fields.pii, "none" otherwise. Populated during perception
    # (surface/web.py's _resolve_attr_names), never guessed from a rationale string.
    sensitivity: Literal["none", "secret", "pii"] = "none"
    # Nesting depth in the accessibility tree. This is the only thing that distinguishes two
    # nameless fields sitting in different table rows (see render() below), so it is carried
    # on the element rather than reconstructed from the raw tree text at render time.
    depth: int = 0
    # Frame segments from the root to this element, outermost first. Root-frame elements get
    # []. A node inside one iframe gets one segment, inside a nested iframe two, and so on.
    frame_path: list[str] = Field(default_factory=list)
    # (role, name) of the nearest ancestors, nearest first, capped at 5. The full chain to the
    # root is noise this app's prompt does not need; Phase 4's relational locator hints do.
    ancestors: list[tuple[str, str]] = Field(default_factory=list)
    # Which strategy produced `name`, so a reader can tell an authored name from an inferred
    # one: "a11y" (the browser computed it), "row_label" / "column_header" (table structure),
    # "attr_name" (placeholder or name attribute), or "none" (no name found at all).
    name_source: str = "none"


class Observation(BaseModel):
    url: str
    title: str
    elements: list[UIElement]

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
