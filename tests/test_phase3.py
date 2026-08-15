"""Phase 3 tests. No browser, no network: everything here runs against fakes or the captured
fixture in tests/fixtures/observation_member_detail.json.
"""

from __future__ import annotations

import json
from pathlib import Path

from understudy.models.observation import Observation, UIElement
from understudy.surface.web import _derive_structural_names

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "observation_member_detail.json"
# Captured by actually driving the live fixture app (login -> member search -> member 12345
# -> "Open Subaccount"), the same way as FIXTURE_PATH, so the "differs after a navigation"
# assertion below compares two real observations rather than a hand-edited copy of one.
SUBACCOUNT_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "observation_subaccount_form.json"


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_digest_stable_when_unchanged_and_differs_after_navigation() -> None:
    fixture = _load_fixture()

    # Re-observing the same, unchanged page must hash identically.
    obs1 = Observation.model_validate(fixture)
    obs2 = Observation.model_validate(json.loads(json.dumps(fixture)))
    assert obs1.digest() == obs2.digest()

    # A value-only change (the balance text updating) must not move the digest: it hashes
    # structure (role, name, name_source, frame_path, depth), never value.
    same_structure = json.loads(json.dumps(fixture))
    for element in same_structure["elements"]:
        if element.get("value") and "1,204.55" in element["value"]:
            element["value"] = "$999.99"
    obs3 = Observation.model_validate(same_structure)
    assert obs3.digest() == obs1.digest()

    # A real navigation changes the tree shape: the member detail page and the subaccount
    # form reached from its "Open Subaccount" link, both captured from the live app, must not
    # hash the same.
    navigated = json.loads(SUBACCOUNT_FIXTURE_PATH.read_text(encoding="utf-8"))
    obs4 = Observation.model_validate(navigated)
    assert obs4.digest() != obs1.digest()


def test_row_rule_derives_account_type_not_savings() -> None:
    """Regression for docs/adr/0004: the account-type <select>'s own containing cell gets its
    accessible name from the selected option ("Savings"), which a naive backward scan would
    wrongly hand to the control. The structural rule must climb to that containing cell first
    and read its PRECEDING sibling cell ("Account Type") instead.
    """
    elements = [
        UIElement(node_id="0", role="table", depth=0),
        UIElement(node_id="1", role="rowgroup", depth=1),
        UIElement(node_id="2", role="row", depth=2),
        UIElement(node_id="3", role="cell", name="Account Type", depth=3),
        UIElement(node_id="4", role="cell", name="Savings", depth=3),
        UIElement(node_id="5", role="combobox", name="", depth=4),
    ]
    parents: list[int | None] = [None, 0, 1, 2, 2, 4]

    _derive_structural_names(elements, parents)

    combobox = elements[5]
    assert combobox.name == "Account Type"
    assert combobox.name != "Savings"
    assert combobox.name_source == "row_label"


def test_render_respects_max_elements_and_reports_the_omission() -> None:
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[UIElement(node_id=str(i), role="cell", name=f"c{i}") for i in range(5)],
    )
    rendered = observation.render(max_elements=3)

    assert "[0]" in rendered
    assert "[2]" in rendered
    assert "[3]" not in rendered
    assert "2 more element(s) omitted" in rendered
    assert "<" not in rendered
    assert ">" not in rendered
