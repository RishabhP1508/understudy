"""WebSurface: Playwright + accessibility-first perception (ARCHITECTURE.md decision 3).

`page.accessibility` does not exist in the installed Playwright version (verified: it was
removed). `page.aria_snapshot(mode="ai")` is the replacement, and one call on the frameset page
already returns the nav frame, the content frame, and a depth-2 iframe in a single tree -- frame
traversal is free, so there is no frame-walking code here. Elements carry a `[ref=...]` handle
that is only valid until the next snapshot; nothing durable ever stores one. The model addresses
an element by its rendered `[index]`, and `_index_to_ref` maps that back to the live ref that
`aria-ref=...` locators need.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from understudy.models.observation import Observation, UIElement
from understudy.surface.base import Action, Click, Key, Navigate, ReadText, Select, Type

_LINE_RE = re.compile(r"^(?P<indent>\s*)- (?P<content>.*)$")
_ATTR_RE = re.compile(r"\[([^\]]*)\]")
_ROLE_RE = re.compile(r"^[a-zA-Z0-9_]+")


def _parse_snapshot(text: str) -> tuple[list[UIElement], dict[str, str]]:
    """Parse one page.aria_snapshot(mode="ai") tree into a flat, indexed element list.

    Line grammar: indent, "- ", role, optional `"name"`, zero or more `[attr]`/`[attr=value]`,
    optional trailing `:`, and optionally an inline value after the colon. A line whose content
    starts with "/" is an element property (e.g. "/url: /members"), not an element, and is
    skipped. Indent step is 2 spaces per nesting level (verified against the live fixture).
    """
    elements: list[UIElement] = []
    refs: dict[str, str] = {}
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        match = _LINE_RE.match(raw_line)
        if not match:
            continue
        depth = len(match.group("indent")) // 2
        content = match.group("content")
        if content.startswith("/"):
            continue
        attrs = _ATTR_RE.findall(content)
        stripped = _ATTR_RE.sub("", content).strip()
        role_match = _ROLE_RE.match(stripped)
        if not role_match:
            continue
        role = role_match.group(0)
        remainder = stripped[role_match.end() :].strip()

        name = ""
        if remainder.startswith('"'):
            end_quote = remainder.find('"', 1)
            if end_quote != -1:
                name = remainder[1:end_quote]
                remainder = remainder[end_quote + 1 :].strip()

        value: str | None = None
        if remainder.startswith(":"):
            inline = remainder[1:].strip()
            if inline:
                if len(inline) >= 2 and inline[0] == '"' and inline[-1] == '"':
                    inline = inline[1:-1]
                value = inline or None

        ref: str | None = None
        states: list[str] = []
        for attr in attrs:
            if attr.startswith("ref="):
                ref = attr.split("=", 1)[1]
            else:
                states.append(attr)

        node_id = str(len(elements))
        elements.append(
            UIElement(
                node_id=node_id, role=role, name=name, value=value, states=states, depth=depth
            )
        )
        if ref is not None:
            refs[node_id] = ref
    return elements, refs


class WebSurface:
    """Playwright-backed Surface, launched headed (CLAUDE.md requires it for the Phase 10
    escalation handoff, so it is never switched to headless for convenience)."""

    def __init__(self, headless: bool = False) -> None:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=headless)
        self._page = self._browser.new_page()
        self._index_to_ref: dict[str, str] = {}
        # Native browser dialogs (window.confirm/alert/prompt) block Playwright's own
        # navigation and action calls until answered. Recording the dialog without dismissing
        # it is deliberate: whether to accept or dismiss is a recovery-policy decision that
        # belongs to Phase 9, not to perception. dialog_events is drained by the evidence
        # logger / agent loop.
        self.dialog_events: list[dict[str, Any]] = []
        self._page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog: Any) -> None:
        self.dialog_events.append({"dialog_type": dialog.type, "message": dialog.message})

    def observe(self) -> Observation:
        snapshot = self._page.aria_snapshot(mode="ai")
        elements, refs = _parse_snapshot(snapshot)
        self._index_to_ref = refs
        return Observation(url=self._page.url, title=self._page.title(), elements=elements)

    def act(self, action: Action) -> str | None:
        if isinstance(action, Navigate):
            self._page.goto(action.url)
            self._page.wait_for_load_state("load")
            return None
        if isinstance(action, Click):
            self._page.locator(f"aria-ref={self._require_ref(action.node_id)}").click()
            self._page.wait_for_load_state("load")
            return None
        if isinstance(action, Type):
            self._page.locator(f"aria-ref={self._require_ref(action.node_id)}").fill(action.text)
            return None
        if isinstance(action, Select):
            self._page.locator(f"aria-ref={self._require_ref(action.node_id)}").select_option(
                action.value
            )
            return None
        if isinstance(action, Key):
            self._page.keyboard.press(action.key)
            return None
        if isinstance(action, ReadText):
            locator = self._page.locator(f"aria-ref={self._require_ref(action.node_id)}")
            text: str = locator.inner_text()
            return text
        raise ValueError(f"unsupported action: {action!r}")

    def screenshot(self, path: Path) -> None:
        """Raw PNG bytes this phase; Phase 5 masks them using aria_snapshot(boxes=True)."""
        self._page.screenshot(path=str(path))

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()

    def _require_ref(self, node_id: str) -> str:
        ref = self._index_to_ref.get(node_id)
        if ref is None:
            raise ValueError(f"no live ref for node_id {node_id!r}; observe() again before acting")
        return ref
