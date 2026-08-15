"""UIElement and Observation: the model's only view onto a live surface.

There is no HTML here, ever. See docs/adr/0002-accessibility-tree-over-screenshots.md for why
perception is built from the accessibility tree instead.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field


class UIElement(BaseModel):
    node_id: str
    role: str
    name: str = ""
    value: str | None = None
    states: list[str] = Field(default_factory=list)
    # Bounds stay None this phase. Phase 5 turns on aria_snapshot(boxes=True) so screenshots
    # can be masked before the PNG bytes are written.
    bounds: list[float] | None = None
    # Nesting depth in the accessibility tree. This is the only thing that distinguishes two
    # nameless fields sitting in different table rows (see render() below), so it is carried
    # on the element rather than reconstructed from the raw tree text at render time.
    depth: int = 0


class Observation(BaseModel):
    url: str
    title: str
    elements: list[UIElement]

    def digest(self) -> str:
        """A stable hash of (role, name, value) across every element.

        Used to detect "nothing changed" between two observations, e.g. after a dead-end click.
        """
        payload = json.dumps(
            [[element.role, element.name, element.value] for element in self.elements],
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def render(self) -> str:
        """Compact indexed text, never HTML.

        [index] is how a tool call addresses an element; the surface maps that index back to a
        live ref at act() time. Indentation preserves adjacency, which is the only way to tell
        two same-named or unnamed fields apart on a form with no <label>.
        """
        lines = [f"URL: {self.url}"]
        for index, element in enumerate(self.elements):
            indent = "  " * element.depth
            piece = element.role
            if element.name:
                piece += f' "{element.name}"'
            line = f"[{index}] {indent}{piece}"
            if element.value:
                line += f": {element.value}"
            lines.append(line)
        return "\n".join(lines)
