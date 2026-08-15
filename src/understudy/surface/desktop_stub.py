"""DesktopSurface: the documented seam for a Windows UI Automation (UIA) backend.

Not implemented. This module exists so the surface-agnostic claim in ARCHITECTURE.md decision 2
is checkable against a second, concrete surface rather than asserted in prose: every concept the
web surface and the artifact schema depend on has a named UIA counterpart below, and every place
that mapping is NOT clean is called out rather than glossed over.

The seam is unchanged: `observe()` returns an `Observation` of `UIElement`s, `act()` takes the
same `Action` union `web.py` does, and a recorded `Capability` names a role and an accessible name,
never a Win32 handle or a coordinate. A replay engine written against the `Surface` protocol does
not know or care which of `WebSurface` or `DesktopSurface` it is holding.

Concept-by-concept mapping, browser (Playwright/Chromium accessibility tree) -> desktop
(UI Automation, via the `pywinauto` or `comtypes` bindings to `IUIAutomation`):

- observe() (the whole tree in one call)
    Web:     one `page.aria_snapshot(mode="ai")` call returns the frameset, every child frame,
             and every nested iframe as a single indented tree (docs/adr/0002).
    Desktop: no single call does this. `IUIAutomation::CreateTreeWalker` (RawViewWalker or
             ControlViewWalker) has to be driven explicitly from a root `AutomationElement`,
             visiting `GetFirstChild`/`GetNextSibling` at every level. `RawViewWalker` sees every
             element UIA knows about; `ControlViewWalker` (the one to use) already filters out
             the layout noise Windows exposes for things like scrollbar thumbs and group
             containers, which is the desktop analogue of the browser's own accessibility
             computation collapsing presentational markup. This is real recursive tree-walking
             code that `web.py` does not need to write, because the browser already did it.

- role
    Web:     the snapshot's role token (`textbox`, `button`, `cell`, ...), which is Chromium's
             computed ARIA role, not necessarily the HTML tag.
    Desktop: `AutomationElement.Current.ControlType`, an enum (`UIA_ButtonControlTypeId`,
             `UIA_EditControlTypeId`, `UIA_ComboBoxControlTypeId`, ...). `UIElement.role` would
             hold a normalized name for these constants, the same normalization `web.py` already
             does implicitly by trusting Chromium's role vocabulary.

- name
    Web:     the parsed accessible name (`"Username"`, `""`, ...).
    Desktop: `AutomationElement.Current.Name`. Computed the same way conceptually (from a
             `LabeledBy` association, from content, or from a name property), but by the Win32
             UIA provider instead of Chromium, so an app with no `LabeledBy` relationship is
             exactly as unlabeled on desktop as this fixture is on the web.

- value
    Web:     the snapshot's inline value after `:` (an input's current text, a select's current
              option).
    Desktop: `ValuePattern.Current.Value` via `IUIAutomationValuePattern`, when the element
             supports `ValuePattern`. Not every control does (a native button does not have a
             "value"); the desktop surface would need to probe
             `GetCurrentPattern(UIA_ValuePatternId)` per element the same way `web.py` reads an
             inline value only when the snapshot line has one.

- bounds
    Web:     None this phase; Phase 5 turns on `aria_snapshot(boxes=True)`.
    Desktop: `AutomationElement.Current.BoundingRectangle`, a screen-space rectangle UIA always
             exposes. Cleaner than the web case, where a box has to be explicitly requested.

- Click
    Web:     `page.locator("aria-ref=...").click()`, a real synthesized input event.
    Desktop: `InvokePattern.Invoke()` via `IUIAutomationInvokePattern` for anything that supports
             it (buttons, menu items). Some legacy Win32 controls expose no `InvokePattern` at
             all; those fall back to `IAccessible` (`LegacyIAccessiblePattern.DoDefaultAction()`),
             which is UIA's own bridge to the older MSAA API, or to a synthesized mouse click at
             the element's `BoundingRectangle` centre as a last resort -- the desktop equivalent
             of this project's own CSS-selector fallback: it works, but it is the least stable
             strategy and would be ranked last for exactly that reason.

- Type
    Web:     `.fill(text)`, which sets the value directly rather than sending keystrokes.
    Desktop: `ValuePattern.SetValue()` via `IUIAutomationValuePattern`, the direct equivalent. A
             control with no `ValuePattern` (some custom-drawn text fields) would need
             `SendKeys`-style synthesized keystrokes instead, which is slower and order-sensitive
             in a way `SetValue` is not -- the desktop analogue of `.fill()` versus `.type()`.

- Select
    Web:     `.select_option(value)` on a `<select>`.
    Desktop: `SelectionItemPattern.Select()` via `IUIAutomationSelectionItemPattern` on the
             target item (e.g. a combo box's list item), not on the combo box itself; the
             combo box exposes `ExpandCollapsePattern` to open the list first. Two patterns
             cooperating where the web has one call is the honest cost of this mapping.

- frame_path
    Web:     iframe ancestors, resolved to real `Frame` objects and named by `frame.name` or
             URL path (docs/adr/0004).
    Desktop: there is no iframe concept, but there is an analogous containment hierarchy: the
             top-level `Window` element (a distinct process/HWND), then `Pane` elements nested
             beneath it (an MDI child window, a docked panel, an embedded ActiveX or WPF island).
             `frame_path` on desktop would record that Window/Pane chain the same way it records
             iframe segments on the web: not because the mechanism is identical, but because both
             are answering the same question -- "which embedded surface is this element actually
             on" -- which is exactly the seam this project's schema is designed to generalize.

- name derivation for unlabeled controls (docs/adr/0004's row/column rule)
    The same problem exists on Win32: a grid built from a bare `DataGrid` or an owner-drawn
    `ListView` routinely has controls with no accessible name, because the caption lives in a
    neighbouring cell, not in a `LabeledBy` relationship. Two things map cleanly, one does not:
      - `GridPattern.GetItem(row, col)` via `IUIAutomationGridPattern` gives an actual (row,
        column) address for a cell in anything that implements `GridPattern` -- a real, richer
        analogue of "climb to the containing cell" that the web surface has to reconstruct from
        indentation because HTML tables carry no such API.
      - `TableItemPattern.GetRowHeaderItems()` / `GetColumnHeaderItems()` via
        `IUIAutomationTableItemPattern` is the direct analogue of `column_header`: it asks the
        control for its header cells instead of walking a preceding sibling row, again richer
        than the web case.
      - What does NOT map cleanly: the fixture's actual failure mode, the caption living in a
        *plain* neighbouring cell of a table with no `GridPattern`/`TableItemPattern` support at
        all (a legacy Win32 app drawing its own grid with static labels and edit controls, no
        `SysListView32` or modern grid control underneath). UIA then exposes flat siblings with
        no row/column semantics whatsoever, and the row-label climb this module's web
        counterpart performs would have nothing to climb: there is no `cell` role and no `row`
        role to anchor on, only a `Pane` full of `Text` and `Edit` siblings at the same
        `TreeWalker` depth. The row/column rule's "the information is present in the tree, just
        not attached to the control" assumption depends on the OS exposing table semantics at
        all, and Win32 does not guarantee that the way HTML `<table>` does. That case would fall
        back to `attr_name`'s desktop analogue: proximity of `BoundingRectangle`s between a
        `Text` element and the nearest `Edit`/`Button` element to its right or below it, which is
        a strictly weaker, coordinate-based heuristic and would be labelled as brittle in the
        artifact the same way this project's CSS fallback already is.
"""

from __future__ import annotations

from understudy.models.observation import Observation
from understudy.surface.base import Action


class DesktopSurface:
    """A `Surface` for a native Windows application, backed by UI Automation.

    Every method raises `NotImplementedError`. This class exists to prove the `Surface` protocol
    is not web-specific by construction, not to run anything; see the module docstring for the
    concept-by-concept mapping that a real implementation would follow.
    """

    def observe(self) -> Observation:
        raise NotImplementedError(
            "desktop perception needs a UIA TreeWalker; see this module's docstring"
        )

    def act(self, action: Action) -> str | None:
        raise NotImplementedError(
            "desktop actions need UIA control patterns (Invoke/SetValue/Select); "
            "see this module's docstring"
        )
