"""WebSurface: Playwright + accessibility-first perception (ARCHITECTURE.md decision 3).

`page.accessibility` does not exist in the installed Playwright version (verified: it was
removed). `page.aria_snapshot(mode="ai")` is the replacement, and one call on the frameset page
already returns the nav frame, the content frame, and a depth-2 iframe in a single tree -- frame
traversal is free, so there is no frame-walking code here. Elements carry a `[ref=...]` handle
that is only valid until the next snapshot; nothing durable ever stores one. The model addresses
an element by its rendered `[index]`, and `_index_to_ref` maps that back to the live ref that
`aria-ref=...` locators need.

This app names almost nothing: form fields are `f1`, `f2`, `f7`, and there is no `<label for=>`
anywhere (docs/adr/0004-name-derivation-for-unlabeled-controls.md). `_derive_structural_names`
recovers a name for those elements from the snapshot's own table structure -- climbing to an
element's containing row cell and reading a neighbouring caption -- rather than scanning
backwards through the flat text, which is measurably wrong (the ADR's account-type example).
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from understudy.models.observation import Observation, UIElement
from understudy.safety.policy import Policy
from understudy.surface.base import Action, Click, Key, Navigate, ReadText, Select, Type

_LINE_RE = re.compile(r"^(?P<indent>\s*)- (?P<content>.*)$")
_ATTR_RE = re.compile(r"\[([^\]]*)\]")
_ROLE_RE = re.compile(r"^[a-zA-Z0-9_]+")

# ADR 0004, ladder step 4: attr_name is only tried for roles a user can actually interact with,
# and only through a live element handle, so it costs a Playwright round trip per element.
_INTERACTIVE_ROLES = {"textbox", "combobox", "button", "checkbox", "radio", "link", "searchbox"}
_ATTR_NAME_CAP = 30

# Bounded window (measured, not guessed -- see docs/adr/0005-child-frame-navigation-wait.md) to
# detect whether a click started a navigation at all, before deciding it did not.
_NAV_DETECT_TIMEOUT_MS = 300


def _match_sensitivity(
    candidates: tuple[str, ...], policy: Policy | None
) -> Literal["none", "secret", "pii"]:
    """Data-driven, never keyword-on-prose: match candidate attribute strings (name, name
    attribute, id, autocomplete) against policy.sensitive_fields, case-insensitive substring.
    Structural signals (type="password") are checked separately by the caller and win outright,
    since they are stronger evidence than any name-based pattern.
    """
    if policy is None:
        return "none"
    haystacks = [candidate.lower() for candidate in candidates if candidate]
    if not haystacks:
        return "none"
    for pattern in policy.sensitive_fields.get("secret", []):
        if any(pattern.lower() in haystack for haystack in haystacks):
            return "secret"
    for pattern in policy.sensitive_fields.get("pii", []):
        if any(pattern.lower() in haystack for haystack in haystacks):
            return "pii"
    return "none"


def _parse_snapshot(text: str) -> tuple[list[UIElement], dict[str, str], list[int | None]]:
    """Parse one page.aria_snapshot(mode="ai") tree into a flat, indexed element list plus a
    parent-index array that reconstructs the tree from indentation.

    Line grammar: indent, "- ", role, optional `"name"`, zero or more `[attr]`/`[attr=value]`,
    optional trailing `:`, and optionally an inline value after the colon. A line whose content
    starts with "/" is an element property (e.g. "/url: /members"), not an element, and is
    skipped. Indent step is 2 spaces per nesting level (verified against the live fixture).

    `parents[i]` is the index of element i's parent, or None at the root. Because the snapshot
    text is a pre-order walk, a parent is always seen (and appended) before its children, so
    `parents[i] < i` always -- later passes exploit that to derive names, frame paths, and
    ancestor chains in a single forward pass with no recursion.
    """
    elements: list[UIElement] = []
    refs: dict[str, str] = {}
    parents: list[int | None] = []
    # Stack of (depth, index) for the current ancestor chain; a new line pops every entry at
    # or below its own depth, so what remains is that line's parent.
    stack: list[tuple[int, int]] = []
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

        index = len(elements)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parent = stack[-1][1] if stack else None
        parents.append(parent)
        stack.append((depth, index))

        node_id = str(index)
        elements.append(
            UIElement(
                node_id=node_id,
                role=role,
                name=name,
                value=value,
                states=states,
                depth=depth,
                name_source="a11y" if name else "none",
            )
        )
        if ref is not None:
            refs[node_id] = ref
    return elements, refs, parents


def _derive_structural_names(elements: list[UIElement], parents: list[int | None]) -> None:
    """ADR 0004, ladder steps 2-3: row_label and column_header. Pure and browser-free -- the
    accessible name, the tree shape, and the parent links are everything this needs.

    The rule is structural, not a backwards text scan: from a nameless element, climb to the
    ancestor `cell` whose own parent is a `row` (this crosses iframe boundaries for free, since
    the snapshot already threads them into one tree), then take that cell's nearest preceding
    sibling cell in the same row that has a name. Only if no preceding sibling in the row has a
    name does it fall back to the cell at the same position in the preceding sibling row.
    """
    children: dict[int, list[int]] = defaultdict(list)
    for index, parent in enumerate(parents):
        if parent is not None:
            children[parent].append(index)

    # containing_row_cell(start) inspects only the PARENT at each climb step, never `start`
    # itself, so when `start` is itself one of these structural roles the climb walks straight
    # past its own row/table and keeps going until it finds a cell belonging to an OUTER
    # table -- in an app nested three tables deep, that lets a nameless inner container
    # inherit an unrelated outer row's caption. These roles are containers, not the
    # interactive/value-bearing leaves a caption is for, so they are never candidates for
    # derivation in the first place rather than patching the climb to special-case `start`.
    _CONTAINER_ROLES = {"cell", "row", "rowgroup", "table"}

    def containing_row_cell(start: int) -> int | None:
        node = start
        while True:
            parent = parents[node]
            if parent is None:
                return None
            grandparent = parents[parent]
            if (
                elements[parent].role == "cell"
                and grandparent is not None
                and elements[grandparent].role == "row"
            ):
                return parent
            node = parent

    for element in elements:
        if element.name:
            continue
        if element.role in _CONTAINER_ROLES:
            continue
        index = int(element.node_id)
        cell_index = containing_row_cell(index)
        if cell_index is None:
            continue
        row_index = parents[cell_index]
        assert row_index is not None
        row_cells = children[row_index]
        position = row_cells.index(cell_index)

        preceding_named = next(
            (
                sibling
                for sibling in reversed(row_cells[:position])
                if elements[sibling].role == "cell" and elements[sibling].name
            ),
            None,
        )
        if preceding_named is not None:
            element.name = elements[preceding_named].name
            element.name_source = "row_label"
            continue

        table_index = parents[row_index]
        if table_index is None:
            continue
        sibling_rows = [c for c in children[table_index] if elements[c].role == "row"]
        row_position = sibling_rows.index(row_index)
        if row_position == 0:
            continue
        previous_row = sibling_rows[row_position - 1]
        previous_row_cells = [c for c in children[previous_row] if elements[c].role == "cell"]
        if position < len(previous_row_cells) and elements[previous_row_cells[position]].name:
            element.name = elements[previous_row_cells[position]].name
            element.name_source = "column_header"


class WebSurface:
    """Playwright-backed Surface, launched headed (CLAUDE.md requires it for the Phase 10
    escalation handoff, so it is never switched to headless for convenience)."""

    def __init__(self, policy: Policy | None = None, headless: bool = False) -> None:
        self._policy = policy
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

        # Navigation guard, installed only when a policy is given (a policy-less WebSurface has
        # nothing to check against). BOTH handlers are required, for a reason
        # that is not obvious: Chromium does NOT create a Route for a redirect hop (verified in
        # Playwright's own source, crNetworkManager.ts: "We do not support intercepting
        # redirects", and in its test suite, page-network-request.spec.ts: "should not work for
        # a redirect and interception"). page.route (a) therefore blocks an INITIAL navigation
        # request -- a meta-refresh, a window.open, a clicked external link -- but cannot block
        # a server-side 302; the page.on("request") listener (b) is what observes that 302 after
        # the fact, so the run can still be aborted at the gate once it has happened.
        self.navigation_violations: list[str] = []
        if policy is not None:
            self._page.route("**/*", self._on_route)
            self._page.on("request", self._on_navigation_request)

    def _on_dialog(self, dialog: Any) -> None:
        self.dialog_events.append({"dialog_type": dialog.type, "message": dialog.message})

    def _record_violation(self, url: str) -> None:
        if url not in self.navigation_violations:
            self.navigation_violations.append(url)

    def _on_route(self, route: Any) -> None:
        request = route.request
        if (
            self._policy is not None
            and request.is_navigation_request()
            and not self._policy.allows_url(request.url)
        ):
            self._record_violation(request.url)
            route.abort("blockedbyclient")
            return
        route.continue_()

    def _on_navigation_request(self, request: Any) -> None:
        if (
            self._policy is not None
            and request.is_navigation_request()
            and not self._policy.allows_url(request.url)
        ):
            self._record_violation(request.url)

    def _frame_segment(self, ref: str, cache: dict[str, str]) -> str:
        """The segment an `iframe` element contributes to a descendant's frame_path: the live
        frame's name if it has one, else its URL path (fact D). Cached per ref so a frame with
        many descendants is resolved once per observation, not once per descendant.
        """
        cached = cache.get(ref)
        if cached is not None:
            return cached
        handle = self._page.locator(f"aria-ref={ref}").element_handle()
        frame = handle.content_frame() if handle is not None else None
        segment = "" if frame is None else (frame.name or urlparse(frame.url).path)
        cache[ref] = segment
        return segment

    def _resolve_frame_paths(
        self, elements: list[UIElement], refs: dict[str, str], parents: list[int | None]
    ) -> None:
        """DP over parents (parent index < child index, see _parse_snapshot): each element's
        frame_path is its parent's frame_path, plus one more segment if the parent is itself an
        `iframe`. One frame resolution per unique iframe ref, however many descendants it has.
        """
        segment_cache: dict[str, str] = {}
        computed: list[list[str]] = []
        for index, element in enumerate(elements):
            parent = parents[index]
            if parent is None:
                path = []
            else:
                path = list(computed[parent])
                parent_element = elements[parent]
                if parent_element.role == "iframe":
                    parent_ref = refs.get(parent_element.node_id)
                    if parent_ref is not None:
                        path.append(self._frame_segment(parent_ref, segment_cache))
            computed.append(path)
            element.frame_path = path

    def _resolve_attr_names(self, elements: list[UIElement], refs: dict[str, str]) -> None:
        """ADR 0004, ladder step 4: placeholder, then the name attribute, read through the
        element's own ref, plus (Phase 5) the type/autocomplete/id attributes sensitivity needs.
        One round trip per interactive element, capped, because each check costs a round trip.

        Changed in Phase 5: this used to `continue` past any element that already had a name, on
        the theory that naming was the only thing this method did. Sensitivity now has to be
        resolved for EVERY interactive element regardless, because this app's password field
        already gets name="Password" from _derive_structural_names (the row-label rule) before
        this method ever runs -- if the skip condition still included `element.name`, the
        password field's `type="password"` would never be read at all, and the strongest, most
        structural sensitivity signal this system has would be silently lost for exactly the
        field it matters most for. The cap and the "only ASSIGN a name if one is missing" rule are
        both unchanged.
        """
        attempts = 0
        for element in elements:
            if attempts >= _ATTR_NAME_CAP:
                break
            if element.role not in _INTERACTIVE_ROLES:
                continue
            ref = refs.get(element.node_id)
            if ref is None:
                continue
            attempts += 1
            attrs = self._page.locator(f"aria-ref={ref}").evaluate(
                "el => ({p: el.placeholder || '', n: el.getAttribute('name') || '', "
                "t: el.type || '', a: el.autocomplete || '', id: el.id || ''})"
            )
            placeholder = attrs.get("p") or ""
            name_attr = attrs.get("n") or ""
            input_type = attrs.get("t") or ""
            autocomplete = attrs.get("a") or ""
            element_id = attrs.get("id") or ""

            if not element.name:
                if placeholder:
                    element.name = placeholder
                    element.name_source = "attr_name"
                elif name_attr:
                    element.name = name_attr
                    element.name_source = "attr_name"

            if input_type == "password":
                element.sensitivity = "secret"  # structural: the strongest signal there is
            else:
                element.sensitivity = _match_sensitivity(
                    (element.name, name_attr, element_id, autocomplete), self._policy
                )

    def _attach_ancestors(self, elements: list[UIElement], parents: list[int | None]) -> None:
        """DP over parents: each element's ancestors is its parent plus the parent's own
        ancestors, capped at 5. Run last, so an ancestor's entry reflects its final derived
        name, not just its raw accessible name.
        """
        for index, element in enumerate(elements):
            parent = parents[index]
            if parent is None:
                element.ancestors = []
            else:
                parent_element = elements[parent]
                element.ancestors = (
                    [(parent_element.role, parent_element.name)] + parent_element.ancestors
                )[:5]

    def observe(self) -> Observation:
        snapshot = self._page.aria_snapshot(mode="ai")
        elements, refs, parents = _parse_snapshot(snapshot)
        self._index_to_ref = refs
        _derive_structural_names(elements, parents)
        self._resolve_frame_paths(elements, refs, parents)
        self._resolve_attr_names(elements, refs)
        self._attach_ancestors(elements, parents)
        return Observation(url=self._page.url, title=self._page.title(), elements=elements)

    def act(self, action: Action) -> str | None:
        if isinstance(action, Navigate):
            self._page.goto(action.url)
            self._page.wait_for_load_state("load")
            return None
        if isinstance(action, Click):
            ref = self._require_ref(action.node_id)
            self._click_and_settle(ref)
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

    def _click_and_settle(self, ref: str) -> None:
        """Click, then wait for whatever the click actually did -- which, in a frameset app,
        is almost always a CHILD FRAME navigating rather than the top-level page.

        THE RACE: `page.wait_for_load_state("load")` only covers the top-level page, and
        measured directly against this fixture, calling `frame.wait_for_load_state("load")` on
        the child frame right after the click *also* fails -- it returns near-instantly on
        STALE state, because the click's own protocol round trip completes before Playwright
        has delivered the `framenavigated` protocol event for the frame back to this process.
        Nothing forces that delivery except another blocking call that pumps the connection.
        `expect_event("framenavigated", ...)` is that blocking call: it starts listening
        *before* the click runs (so it cannot miss a navigation that starts within the click's
        own round trip) and, on exit, blocks until the event is actually delivered or the bound
        elapses. Only once it fires do we know which frame to wait on, and only then does that
        frame's own `.url` and `wait_for_load_state("load")` reflect the new document rather
        than the old one.

        `expect_navigation()` wrapped around every click is the wrong tool here: with no
        predicate it is page-scoped (misses child-frame navigations entirely) and, when a click
        causes no navigation at all -- typing never reaches this method, but a non-navigating
        click does -- it blocks out its full default timeout (30s) every single time, which is
        a fixed sleep wearing a condition-wait's clothes. `expect_event` has the same shape, and
        there is no way to prove a negative ("nothing will ever navigate") without waiting some
        bound, so a non-navigating click always pays this bound -- but the bound itself is
        small and measured, not guessed: `framenavigated` arrives 15-30ms after the click on
        this fixture (measured directly, docs/adr/0005), so 300ms is >10x headroom for a slower
        machine while still cutting the cost of a non-navigating click by 3-6x versus a naive
        1-second guess (measured: ~1000ms/click -> ~300ms/click).
        """
        frame = None
        try:
            with self._page.expect_event(
                "framenavigated", timeout=_NAV_DETECT_TIMEOUT_MS
            ) as nav_info:
                self._page.locator(f"aria-ref={ref}").click()
            frame = nav_info.value
        except PlaywrightTimeoutError:
            pass  # no navigation started within the bound: nothing to settle beyond the page.
        if frame is not None:
            frame.wait_for_load_state("load")
        self._page.wait_for_load_state("load")

    @property
    def url(self) -> str:
        return self._page.url

    def screenshot_bytes(self) -> bytes:
        """Raw PNG bytes. Masking happens one layer up, in EvidenceLogger.screenshot -- this
        method never writes anything itself, so there is exactly one place PNG bytes hit disk
        (safety/redact.py's redact_screenshot, called from evidence/logger.py).

        `page.screenshot()` captures the viewport at deviceScaleFactor 1 (this app never sets
        it otherwise), so PNG pixels equal CSS pixels and an element's `bounding_box()` lands on
        the right pixels directly; a non-default scale factor would need those coordinates
        scaled before drawing a mask.
        """
        result: bytes = self._page.screenshot()
        return result

    def fill_bounds(self, elements: list[UIElement]) -> None:
        """Resolve a live bounding box for each of the given elements, through their aria-ref.
        Called ONLY for elements a caller already knows are sensitive (never for a whole
        observation), so perception itself stays cheap -- most elements never pay this cost.
        """
        for element in elements:
            ref = self._index_to_ref.get(element.node_id)
            if ref is None:
                continue
            box = self._page.locator(f"aria-ref={ref}").bounding_box()
            if box is not None:
                element.bounds = [box["x"], box["y"], box["width"], box["height"]]

    def dom_snapshot(self) -> str:
        """The raw top-level document HTML (`page.content()`). Evidence for a HUMAN debugging a
        failure, never perception for the model -- the model only ever sees observe()'s indexed
        accessibility text (ARCHITECTURE.md decision 3); this exists solely so
        EvidenceLogger.capture_failure has a richer signal to attach to a HardFailure (R5).
        """
        content: str = self._page.content()
        return content

    @property
    def tracing(self) -> Any:
        """Tracing lives on the BrowserContext, not the Page. `evidence/logger.py` starts and
        stops it around a whole run; this property is the seam that lets it do so without
        reaching into a private attribute.
        """
        return self._page.context.tracing

    def close(self) -> None:
        self._browser.close()
        self._playwright.stop()

    def _require_ref(self, node_id: str) -> str:
        ref = self._index_to_ref.get(node_id)
        if ref is None:
            raise ValueError(f"no live ref for node_id {node_id!r}; observe() again before acting")
        return ref
