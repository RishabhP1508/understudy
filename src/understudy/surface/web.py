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
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from understudy.models.intervention import HumanAction
from understudy.models.observation import Observation, UIElement
from understudy.safety.policy import Policy
from understudy.safety.redact import classify_field_sensitivity
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

# R6 human-action capture (task B): the cap on how many raw DOM events a handoff can accumulate
# in window.sessionStorage before the oldest are dropped -- named here, not buried in the
# injected script, so the docstring that promises "say what the cap is" has one number to point
# at. 500 is generous for a manual handoff (a human clicking/typing through a stuck form is
# nowhere near this many discrete DOM events) while still bounding memory for a handoff nobody
# ends.
_HUMAN_ACTION_CAP = 500

# Round J: the bound on the settle wait `drain_human_actions` performs when a read fails because
# a navigation destroyed the execution context (see that method's own docstring). Reuses
# replay/recovery.py's `_WAIT_TIMEOUT_MS` value for the identical underlying primitive ("wait for
# an in-flight navigation to settle") rather than inventing a second magic number for the same
# wait.
_HUMAN_ACTION_DRAIN_SETTLE_TIMEOUT_MS = 8000

# The sentinel written in place of a captured value, on BOTH layers that ever suppress one (this
# script's own `record()`, for a raw `input type="password"`; `WebSurface.drain_human_actions`'s
# Python-side second layer, for a field whose NAME classify_field_sensitivity calls secret/pii).
# Deliberately NOT an empty string: empty is what an untouched or genuinely-cleared field looks
# like, and a reviewer reading `evidence/interventions/*.json` has to be able to tell "this value
# was never captured on purpose" apart from "the human typed nothing here" (see this module's own
# module docstring reference and the task that added this -- a human's password was previously
# captured one keystroke at a time in plain text, because neither redaction rule fires on a value
# nobody ever registered as a secret: R1 needs a declared capability parameter, which a human's own
# typing during a handoff never is, and R3 needs a credential-shaped literal, which "hunter2" is
# not). Suppressing after the fact would also be useless here: masking only the FINAL value would
# still leave every keystroke-by-keystroke prefix sitting in the record.
_HUMAN_ACTION_SUPPRESSED = "[SUPPRESSED]"

# Kept deliberately tiny (ladder step: the DOM traversal that only JS can do -- tagName, input
# type, and the best name-ish string visible from the element -- stays in JS; translating that
# into the project's role vocabulary happens in Python, in `_human_action_role`, where the
# vocabulary already lives). Stored in window.sessionStorage rather than a plain JS variable so
# it SURVIVES navigation and reload within the tab (a plain variable is wiped by the next
# navigation's fresh JS environment); add_init_script re-installs the listeners on every
# navigation, sessionStorage carries the accumulated data across them.
#
# `record()` already reads `el.type` into `it` for every event, for the role mapping
# (`_human_action_role`) to use later -- suppressing the VALUE for a password field here, at the
# point of capture, means the real keystrokes never leave the page at all, rather than being
# captured and redacted afterward (see `_HUMAN_ACTION_SUPPRESSED`'s own docstring for why "after
# the fact" cannot work for this specific case). This is layer one of two; layer two
# (`WebSurface.drain_human_actions`) catches a sensitive field that is not `type="password"` at
# all, by the same NAME classification the rest of this codebase already uses.
_HUMAN_ACTION_CAPTURE_SCRIPT = """
(() => {
  const CAP = %d;
  const SUPPRESSED = %r;
  const KEY = "__understudy_human_actions";
  function push(rec) {
    try {
      const raw = window.sessionStorage.getItem(KEY);
      const arr = raw ? JSON.parse(raw) : [];
      arr.push(rec);
      while (arr.length > CAP) arr.shift();
      window.sessionStorage.setItem(KEY, JSON.stringify(arr));
    } catch (e) { /* sessionStorage unavailable (e.g. a sandboxed frame): drop silently */ }
  }
  function bestName(el) {
    if (!el || typeof el.getAttribute !== "function") return "";
    let v = el.getAttribute("aria-label");
    if (v) return v.trim();
    v = el.getAttribute("placeholder");
    if (v) return v.trim();
    v = el.getAttribute("name");
    if (v) return v.trim();
    if (el.id) {
      const label = document.querySelector("label[for='" + el.id + "']");
      if (label && label.innerText) return label.innerText.trim();
    }
    const enclosing = el.closest ? el.closest("label") : null;
    if (enclosing && enclosing.innerText) return enclosing.innerText.trim();
    const tag = (el.tagName || "").toLowerCase();
    if ((tag === "button" || tag === "a") && el.innerText) return el.innerText.trim();
    return "";
  }
  function record(kind, el) {
    const inputType = el ? (el.type || "") : "";
    const isPassword = inputType.toLowerCase() === "password";
    push({
      k: kind,
      t: el ? (el.tagName || "").toLowerCase() : "",
      it: inputType,
      n: el ? bestName(el) : "",
      v: isPassword ? SUPPRESSED : (el && el.value !== undefined ? String(el.value) : null),
      u: null,
      at: new Date().toISOString(),
    });
  }
  document.addEventListener("click", (e) => record("click", e.target), true);
  document.addEventListener("input", (e) => record("input", e.target), true);
  document.addEventListener("change", (e) => record("change", e.target), true);
  push({k: "navigate", t: "", it: "", n: "", v: null, u: location.href,
        at: new Date().toISOString()});
})();
"""

# Maps a raw DOM `input.type` onto the project's normalized role vocabulary
# (models/observation.py, policy.allowed_roles) -- anything not listed here falls back to
# "textbox" in `_human_action_role` below, which covers text/email/password/... input types with
# no special-cased role of their own.
_INPUT_TYPE_TO_ROLE: dict[str, str] = {
    "checkbox": "checkbox",
    "radio": "radio",
    "search": "searchbox",
    "submit": "button",
    "button": "button",
    "reset": "button",
    "image": "button",
}


def _human_action_role(tag: str, input_type: str) -> str:
    """Raw DOM tag/input-type -> this project's normalized role vocabulary, done here in Python
    rather than in the injected script (HumanAction's own docstring: this is the whole point of
    recording a human's actions in the SAME terms as the agent's own recorded steps)."""
    tag = tag.lower()
    input_type = input_type.lower()
    if tag == "select":
        return "combobox"
    if tag == "option":
        return "option"
    if tag == "a":
        return "link"
    if tag == "button":
        return "button"
    if tag == "textarea":
        return "textbox"
    if tag == "input":
        return _INPUT_TYPE_TO_ROLE.get(input_type, "textbox")
    return tag


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
        # Round H (H1): installed HERE, unconditionally, for every surface -- not by the two
        # runners that used to be expected to remember to call `install_human_action_capture()`
        # themselves. That was the actual defect: it had exactly one caller in the whole tree and
        # it was a test, so `drain_human_actions` always returned `[]` in both real execution
        # paths. `__init__` is the only place that is true by construction for every surface a
        # caller can build, including one a test constructs directly -- and `add_init_script`
        # must be registered before any page loads for the listener to be present on the first
        # document, which is exactly what happens here, before any `Navigate` action can run. The
        # cost is one `add_init_script` call and a little `sessionStorage` bookkeeping per surface,
        # paid whether or not a handoff ever happens -- worth it to remove a whole class of silent
        # failure (escalation/control.py's `SessionBroker.escalate()` now drains unconditionally
        # too, and depends on this always having run).
        self.install_human_action_capture()
        self._index_to_ref: dict[str, str] = {}
        # Native browser dialogs (window.confirm/alert/prompt) block Playwright's own
        # navigation and action calls until answered. `dialog_policy` is the recovery-policy
        # seam (Phase 9): None (the default) means record-only, which preserves discovery's
        # behaviour exactly -- the dialog is recorded and left open. A caller (replay/recovery.py)
        # that sets this to a closure gets a real per-run attempt cap for free: once its budget is
        # spent the closure returns "none" and the next dialog genuinely blocks, the same as
        # discovery today. dialog_events is drained by the evidence logger / agent loop.
        self.dialog_events: list[dict[str, Any]] = []
        self.dialog_policy: Callable[[dict[str, Any]], str] | None = None
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

        # In-flight navigation tracking, installed UNCONDITIONALLY -- independent of the
        # policy-only guard above, which answers a different question (is this URL allowed).
        # This answers "is a navigation still in progress right now", which `last_navigation`
        # and `wait_for_navigation_to_settle` both read off `_pending_navigations` below.
        self._pending_navigations: set[Any] = set()
        self.last_navigation: Literal["none", "settled", "in_flight"] = "none"
        self._page.on("request", self._on_request_started)
        self._page.on("requestfinished", self._on_request_settled)
        self._page.on("requestfailed", self._on_request_settled)

    def _on_dialog(self, dialog: Any) -> None:
        event = {"dialog_type": dialog.type, "message": dialog.message}
        decision = self.dialog_policy(event) if self.dialog_policy is not None else "none"
        event["handled"] = decision
        self.dialog_events.append(event)
        if decision == "dismiss":
            dialog.dismiss()
        elif decision == "accept":
            dialog.accept()
        # "none" leaves the dialog open, exactly as before this seam existed.

    def _on_request_started(self, request: Any) -> None:
        if request.is_navigation_request():
            self._pending_navigations.add(request)

    def _on_request_settled(self, request: Any) -> None:
        self._pending_navigations.discard(request)

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
        # D3 (Phase 8): every currently loaded URL, shell plus every child frame -- the same
        # values `urls()` reports and `PolicyGate.dispatch` already checks -- so a `url_matches`
        # checkpoint can be verified against the real screen identity (a child frame) rather than
        # a frameset's constant shell URL (docs/adr/0005).
        return Observation(
            url=self._page.url, title=self._page.title(), elements=elements, urls=self.urls()
        )

    def act(self, action: Action) -> str | None:
        """`last_navigation` (Literal["none", "settled", "in_flight"]) records what this action
        did to navigation, an OPTIONAL surface capability read via `getattr` -- never added to
        the Surface protocol itself. "settled": a navigation started and finished (Navigate
        always; Click when the framenavigated event fired within the bound and that frame's own
        load state was reached). "in_flight": a Click's bound elapsed with a navigation request
        still outstanding after the top-level `wait_for_load_state("load")` call -- a slow CHILD
        FRAME load, since a slow TOP-LEVEL navigation is already absorbed by that same call and
        correctly reports "settled". "none": nothing navigated at all (every other action kind,
        or a Click that did not start one).
        """
        if isinstance(action, Navigate):
            self._page.goto(action.url)
            self._page.wait_for_load_state("load")
            self.last_navigation = "settled"
            return None
        if isinstance(action, Click):
            ref = self._require_ref(action.node_id)
            self._click_and_settle(ref)
            return None
        if isinstance(action, Type):
            self._page.locator(f"aria-ref={self._require_ref(action.node_id)}").fill(action.text)
            self.last_navigation = "none"
            return None
        if isinstance(action, Select):
            self._page.locator(f"aria-ref={self._require_ref(action.node_id)}").select_option(
                action.value
            )
            self.last_navigation = "none"
            return None
        if isinstance(action, Key):
            self._page.keyboard.press(action.key)
            self.last_navigation = "none"
            return None
        if isinstance(action, ReadText):
            locator = self._page.locator(f"aria-ref={self._require_ref(action.node_id)}")
            text: str = locator.inner_text()
            self.last_navigation = "none"
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

        Sets `last_navigation` (see act()'s docstring): "settled" once the navigated frame's own
        load state is reached; otherwise, evaluated AFTER the page-level `wait_for_load_state`
        below, "in_flight" if `_pending_navigations` is non-empty (a slow child-frame load still
        outstanding), else "none".
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
            self.last_navigation = "settled"
        self._page.wait_for_load_state("load")
        if frame is None:
            self.last_navigation = "in_flight" if self._pending_navigations else "none"

    def wait_for_navigation_to_settle(self, timeout_ms: float) -> bool:
        """An explicit condition wait for a navigation `last_navigation` already reported
        "in_flight" -- never a sleep. Returns True immediately if nothing is pending. Otherwise
        waits for the next `framenavigated` event, then that frame's own load state, then the
        top-level page's, and returns True; a timeout anywhere in that chain means the
        navigation did not settle within the bound, and this returns False.

        Sets `last_navigation = "settled"` on a successful wait (Phase 9 fix, measured live
        against the fixture's own `slow_load` injection): without this, a caller re-checking
        `last_navigation` after a successful wait still saw "in_flight" -- the value `act()` set
        before this method ever ran -- so replay/recovery.py's `navigation_still_in_flight`
        trigger kept re-firing on every subsequent evaluation pass even though the navigation had
        already genuinely settled, burning an entire step's recovery budget on waits that had
        already succeeded. Left as "in_flight" only when the wait itself times out (`False`),
        which is the one case where the caller's original observation is still accurate.
        """
        if not self._pending_navigations:
            self.last_navigation = "settled"
            return True
        try:
            frame = self._page.wait_for_event("framenavigated", timeout=timeout_ms)
            frame.wait_for_load_state("load")
            self._page.wait_for_load_state("load")
            self.last_navigation = "settled"
            return True
        except PlaywrightTimeoutError:
            return False

    def pause(self, ms: float) -> None:
        """The ONE deliberate timed delay anywhere in this system. It exists solely so a retry
        rule (replay/recovery.py) can space its attempts with real exponential backoff, and it
        lives here rather than under `replay/` because ARCHITECTURE.md decision 18 bans fixed
        sleeps there: a retry backoff is a deliberate delay, not a condition wait, and hiding it
        behind a fake condition would be worse than naming it honestly.
        """
        self._page.wait_for_timeout(ms)

    def reload(self) -> None:
        self._page.reload()
        self._page.wait_for_load_state("load")
        self.last_navigation = "settled"

    @property
    def url(self) -> str:
        return self._page.url

    def urls(self) -> list[str]:
        """The shell plus every child frame, deduplicated, order preserved. Measured live
        against this fixture: `page.url` stays `http://127.0.0.1:5055/app` for the whole
        session (the frameset shell never reloads -- the same fact docs/adr/0005 already
        recorded for click waits), while the actual content lives in `self._page.frames`
        (`navframe` at `/nav`, `contentframe` at whatever route the user last navigated to
        inside it). `PolicyGate.dispatch` needs every one of these, not just the shell.
        """
        seen: list[str] = []
        for frame_url in [self._page.url, *(frame.url for frame in self._page.frames)]:
            if frame_url not in seen:
                seen.append(frame_url)
        return seen

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

    def install_human_action_capture(self) -> None:
        """Install the document-level click/input/change listeners (task B, R6 "record what the
        human did") via `add_init_script` rather than a one-off `evaluate` call: an init script
        re-runs on every navigation this page (and every child frame) makes, so the capture
        survives a human clicking through several pages during a handoff -- the normal case --
        where a one-off `evaluate` would only ever see whatever page happened to be loaded the
        moment it ran. Called once, unconditionally, from `__init__` (round H) -- see that
        docstring for why a call site a caller has to remember is exactly the defect this fixes.

        CANNOT see a native browser dialog: a human answering a `window.confirm` produces no DOM
        event at all -- see `HumanAction`'s own docstring; that case is evidenced instead by
        `dialog_events`' own `handled` value.
        """
        self._page.add_init_script(
            _HUMAN_ACTION_CAPTURE_SCRIPT % (_HUMAN_ACTION_CAP, _HUMAN_ACTION_SUPPRESSED)
        )

    def _read_human_action_buffer(self) -> list[dict[str, Any]]:
        """One `page.evaluate()` round trip: read the captured array out of the MAIN FRAME's
        `sessionStorage` and clear it. See `drain_human_actions`'s own docstring (Finding 1) for
        why the main frame is the right read target even when the human acted inside a child
        frame, and (Finding 2) for the navigation race this call can lose."""
        result: list[dict[str, Any]] = self._page.evaluate(
            "() => { const k = '__understudy_human_actions'; "
            "const v = window.sessionStorage.getItem(k); "
            "window.sessionStorage.removeItem(k); "
            "return v ? JSON.parse(v) : []; }"
        )
        return result

    def drain_human_actions(self) -> list[HumanAction]:
        """Read the captured array out of `sessionStorage`, clear it, and map each raw record
        into a typed `HumanAction`. The tag/input-type -> role translation happens here, in
        Python (`_human_action_role`), not in the injected script, which stays as small as the
        ladder allows.

        FINDING 1 -- the main frame is the CORRECT read target, not a wrong-frame bug: this
        app's frameset shell and its content frame are same-origin, and `sessionStorage` is
        shared across every same-origin frame in one tab. Measured directly against this
        fixture: a single `page.evaluate()` on the shell (this method never touches a child
        frame) returned three actions that were actually performed *inside* `contentframe` (an
        `input` and a `change` on `f7`, and a `click` on "Search"). So there is no frame-walking
        to add here, and no data loss from reading the shell instead of whichever frame the
        human happened to be looking at.

        FINDING 2 -- the real defect this closes: a human's actual click is not wrapped in this
        surface's own `_click_and_settle` wait at all, because a real human clicks the visible
        browser window directly; Playwright only ever *observes* that click, via the page-level
        request listeners `__init__` installs unconditionally. If the human's LAST action before
        control comes back started a navigation, this method's own read can land in the instant
        between the OLD document's execution context being torn down and the NEW one existing --
        Playwright surfaces that as `Error: ... Execution context was destroyed, most likely
        because of a navigation`, for either the main frame or a child frame depending on which
        one the human's last action navigated (reproduced against this fixture for both).
        `sessionStorage` itself survives the navigation intact (the same fact Finding 1 measures
        for frames also holds across a reload), so waiting for the navigation to settle before
        reading costs nothing and loses nothing.

        The fix: read the buffer; if that read raises because a navigation tore down the
        execution context it was reading from, wait for the navigation to settle
        (`wait_for_navigation_to_settle`, this class's own condition wait, never a fixed sleep)
        and read once more. There is no settle call before the first read -- a raw, unwrapped
        human click leaves `_pending_navigations` still empty at that point, because Playwright
        has not yet been told the resulting request even started, so a settle call placed there
        has nothing to wait on and cannot prevent the race. The retry is capped at one: a second
        failure past that point is treated as genuine and propagates. Both the wait and the
        retry are bounded, so this can neither hang the run nor loop.
        """
        try:
            raw = self._read_human_action_buffer()
        except PlaywrightError:
            self.wait_for_navigation_to_settle(_HUMAN_ACTION_DRAIN_SETTLE_TIMEOUT_MS)
            raw = self._read_human_action_buffer()
        actions: list[HumanAction] = []
        for item in raw:
            kind = item.get("k", "click")
            role = (
                ""
                if kind == "navigate"
                else _human_action_role(item.get("t", ""), item.get("it", ""))
            )
            name = item.get("n") or ""
            value = item.get("v")
            # Layer two of two (see _HUMAN_ACTION_SUPPRESSED's own docstring): the injected
            # script's `record()` already suppresses a raw `input type="password"` at the
            # source, but a sensitive field is not always typed "password" (an SSN or account
            # number field is plain text) -- this reuses the project's own
            # `classify_field_sensitivity`, never a second, separately-maintained keyword list,
            # so the two layers can never disagree about which names count as sensitive.
            if value is not None and classify_field_sensitivity(name) != "none":
                value = _HUMAN_ACTION_SUPPRESSED
            actions.append(
                HumanAction(
                    kind=kind,
                    role=role,
                    name=name,
                    value=value,
                    url=item.get("u"),
                    at=item.get("at", ""),
                )
            )
        return actions

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
