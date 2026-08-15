"""Phase 4 tests: the ranked locator strategy list (docs/adr/0006). No browser, no network, no
API key.

Ground truth measured against the two captured Observations
(tests/fixtures/observation_member_detail.json, tests/fixtures/observation_subaccount_form.json):
there are zero duplicate (role, name) pairs among named-or-interactive elements in either one, so
on this real data ROLE_NAME_EXACT is unique every time and never sees two candidates. Tests that
exercise ambiguity, cross-frame scoping, or the DOM_FALLBACK rung therefore build a small
CONSTRUCTED Observation by hand instead of inventing a fixture case that does not occur; each such
test says so in its docstring.

The round-trip test below covers EVERY element in both fixtures, not a filtered subset (round 3
fix: an earlier version filtered to "named or interactive" elements, which happened to be exactly
the 12-of-36 that passed while 15 of 36 (member_detail) and 17 of 37 (subaccount_form) unnamed
container elements silently failed underneath the filter). Round 3 also fixed the real bug behind
those 32 failures: describe() computed `ordinal` within the role+name pool while
_strategy_role_ordinal indexed the role-only pool, so a recorded ordinal indexed a different
position at replay time than it meant at record time (see locator._role_pool). With that shared,
all 36 and all 37 elements now round-trip; see test_round_trip_covers_every_element_all_pass below
for the honest count.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from understudy.models.artifact import Checkpoint
from understudy.models.observation import Observation, UIElement
from understudy.replay.engine import _finish_result
from understudy.surface.locator import (
    RelationalHint,
    ResolutionStrategy,
    TargetDescriptor,
    describe,
    resolve,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

MEMBER_DETAIL = Observation.model_validate_json(
    (FIXTURES_DIR / "observation_member_detail.json").read_text(encoding="utf-8")
)
SUBACCOUNT_FORM = Observation.model_validate_json(
    (FIXTURES_DIR / "observation_subaccount_form.json").read_text(encoding="utf-8")
)
_FIXTURES = {"member_detail": MEMBER_DETAIL, "subaccount_form": SUBACCOUNT_FORM}

def _round_trip_targets() -> list[tuple[str, str]]:
    """EVERY element in both fixtures -- no interactive-role or has-a-name filter. Filtering to a
    subset that happens to pass is exactly the mistake round 3 found: it hid 15 of 36
    (member_detail) and 17 of 37 (subaccount_form) real failures underneath a green test."""
    return [
        (fixture_name, element.node_id)
        for fixture_name, observation in _FIXTURES.items()
        for element in observation.elements
    ]


_ROUND_TRIP_TARGETS = _round_trip_targets()


# --------------------------------------------------------------------------------------
# 1. Round trip: the strongest test. EVERY element in both captured fixtures, filtered or
# unfiltered, must describe() then resolve() back to itself. Parametrized so a failure names the
# exact element (fixture + node_id), not just "some element somewhere". Honest result (round 3,
# after the ordinal-pool fix in locator._role_pool): all 36 + all 37 = 73 of 73 round-trip. None
# are excluded to make this true.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name, node_id",
    _ROUND_TRIP_TARGETS,
    ids=[f"{name}:{node_id}" for name, node_id in _ROUND_TRIP_TARGETS],
)
def test_round_trip_covers_every_element_all_pass(fixture_name: str, node_id: str) -> None:
    observation = _FIXTURES[fixture_name]
    element = next(e for e in observation.elements if e.node_id == node_id)

    descriptor = describe(element, observation)
    resolution = resolve(descriptor, observation)

    assert resolution.element is not None, (
        f"{fixture_name} node {node_id} ({element.role!r} name={element.name!r}) did not "
        f"resolve; attempts={[a.model_dump() for a in resolution.attempts]}"
    )
    assert resolution.element.node_id == node_id


def test_round_trip_rank_distribution_is_reported() -> None:
    """Run-and-report, not a stronger correctness gate than the parametrized test above: the
    distribution of which rank each round-tripped element actually resolved at."""
    distribution: Counter[int] = Counter()
    for fixture_name, node_id in _ROUND_TRIP_TARGETS:
        observation = _FIXTURES[fixture_name]
        element = next(e for e in observation.elements if e.node_id == node_id)
        resolution = resolve(describe(element, observation), observation)
        assert resolution.rank is not None
        distribution[resolution.rank] += 1
    print(
        f"\nround-trip rank distribution over {len(_ROUND_TRIP_TARGETS)} elements: "
        f"{dict(sorted(distribution.items()))}"
    )


# --------------------------------------------------------------------------------------
# 2. A drifted name falls through to a later rung and reports which rank it achieved.
# --------------------------------------------------------------------------------------


def test_descriptor_whose_name_no_longer_matches_falls_through_to_role_ordinal() -> None:
    """CONSTRUCTED: simulates a caption rename after recording, with the app otherwise
    unchanged. The descriptor still carries the ordinal captured when this control was one of
    two same-named buttons, so both name-based rungs report zero candidates and it resolves via
    ROLE_ORDINAL, reporting exactly which rank it achieved.
    """
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[
            UIElement(node_id="0", role="button", name="Approve"),
            UIElement(node_id="1", role="button", name="Approve"),
        ],
    )
    recorded = TargetDescriptor(role="button", name="Approve", ordinal=1)
    drifted = recorded.model_copy(update={"name": "Confirm"})

    resolution = resolve(drifted, observation)

    assert resolution.element is not None
    assert resolution.element.node_id == "1"
    assert resolution.strategy_used == ResolutionStrategy.ROLE_ORDINAL
    assert resolution.rank == 5
    assert resolution.attempts[0].strategy == ResolutionStrategy.ROLE_NAME_EXACT
    assert resolution.attempts[0].candidate_count == 0
    assert resolution.attempts[1].candidate_count == 0


# --------------------------------------------------------------------------------------
# 3. CONSTRUCTED: same (role, name) in two different frames resolves via ROLE_NAME_SCOPED.
# --------------------------------------------------------------------------------------


def test_same_role_name_in_different_frames_resolves_via_scope_not_ordinal() -> None:
    """CONSTRUCTED: neither captured fixture has the same (role, name) pair in two different
    frames (fact C), so this is built by hand to exercise ROLE_NAME_SCOPED specifically."""
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[
            UIElement(node_id="a", role="button", name="Submit", frame_path=["frameA"]),
            UIElement(node_id="b", role="button", name="Submit", frame_path=["frameB"]),
        ],
    )
    descriptor = TargetDescriptor(role="button", name="Submit", frame_path=["frameB"])

    resolution = resolve(descriptor, observation)

    assert resolution.element is not None
    assert resolution.element.node_id == "b"
    assert resolution.strategy_used == ResolutionStrategy.ROLE_NAME_SCOPED
    assert resolution.rank == 3


# --------------------------------------------------------------------------------------
# 4. CONSTRUCTED: a genuine (role, name) duplicate is skipped as ambiguous; a later rung wins.
# --------------------------------------------------------------------------------------


def test_genuine_duplicate_role_name_is_skipped_as_ambiguous_and_a_later_strategy_wins() -> None:
    """CONSTRUCTED: fact A says zero duplicate (role, name) pairs occur among named/interactive
    elements in either fixture, so a genuine duplicate has to be built by hand to prove
    ROLE_NAME_EXACT is skipped (not resolved to the first hit) when it is ambiguous."""
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[
            UIElement(node_id="x", role="generic", name="Balance"),
            UIElement(node_id="y", role="generic", name="Balance"),
        ],
    )
    descriptor = TargetDescriptor(role="generic", name="Balance", ordinal=1)

    resolution = resolve(descriptor, observation)

    assert resolution.element is not None
    assert resolution.element.node_id == "y"
    strategies = list(ResolutionStrategy)
    assert resolution.strategy_used is not None
    assert strategies.index(resolution.strategy_used) > strategies.index(
        ResolutionStrategy.ROLE_NAME_EXACT
    )
    assert resolution.strategy_used == ResolutionStrategy.ROLE_ORDINAL

    exact_attempt = resolution.attempts[0]
    assert exact_attempt.strategy == ResolutionStrategy.ROLE_NAME_EXACT
    assert exact_attempt.candidate_count == 2
    assert exact_attempt.skipped_reason is not None


# --------------------------------------------------------------------------------------
# 4b. THE ROUND-3 BUG: describe() must compute `ordinal` in the same pool _strategy_role_ordinal
# indexes (role only, after scope/frame_path), never the role+name pool. The two disagreeing was
# the worst bug this system could ship: a recorded ordinal silently selecting the wrong element.
# --------------------------------------------------------------------------------------


def test_describe_ordinal_pool_matches_role_ordinal_pool_not_role_name_pool() -> None:
    """CONSTRUCTED: [button "Edit"(0), button "Delete"(1), button "Edit"(2)]. describe() on
    element 2 must record ordinal=2 (its position among ALL THREE buttons -- the pool
    _strategy_role_ordinal actually indexes at replay time), not ordinal=1 (its position among
    just the two "Edit" buttons -- the pool the pre-fix describe() used). If the two pools
    disagree, a later name drift on element 2 replays as a click on node 1 ("Delete") instead of
    node 2 ("Edit"): a click on the wrong row's action.
    """
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[
            UIElement(node_id="0", role="button", name="Edit"),
            UIElement(node_id="1", role="button", name="Delete"),
            UIElement(node_id="2", role="button", name="Edit"),
        ],
    )
    element = next(e for e in observation.elements if e.node_id == "2")

    descriptor = describe(element, observation)
    assert descriptor.ordinal == 2  # position among all 3 buttons, not among the 2 "Edit"s

    # Simulate the caption drifting after recording (fact D's mechanism), forcing the walk past
    # both name-based rungs and down to ROLE_ORDINAL.
    drifted = descriptor.model_copy(update={"name": "Modify"})
    resolution = resolve(drifted, observation)

    assert resolution.element is not None
    assert resolution.element.node_id == "2"
    assert resolution.strategy_used == ResolutionStrategy.ROLE_ORDINAL


# --------------------------------------------------------------------------------------
# 5 & 6. CASE 1 and CASE 2 (real, member_detail): the three-way "Savings Balance" collision.
# --------------------------------------------------------------------------------------


def test_case1_savings_balance_value_resolves_on_a_named_strategy_not_ordinal() -> None:
    """CASE 1 (real, member_detail): the node holding $1,204.55. Its name is derived
    ('Savings Balance', name_source='row_label' -- docs/adr/0004), but role alone separates it
    from the cell and the iframe sharing that same name, so it resolves at ROLE_NAME_EXACT,
    rank 1, not by position.
    """
    matches = [e for e in MEMBER_DETAIL.elements if e.value == "$1,204.55"]
    assert len(matches) == 1
    element = matches[0]
    assert element.role == "generic"
    assert element.name == "Savings Balance"

    descriptor = describe(element, MEMBER_DETAIL)
    resolution = resolve(descriptor, MEMBER_DETAIL)

    assert resolution.element is not None
    assert resolution.element.node_id == element.node_id
    assert resolution.strategy_used == ResolutionStrategy.ROLE_NAME_EXACT
    assert resolution.rank == 1


def test_case2_savings_balance_name_has_three_candidates_role_separates_them() -> None:
    """CASE 2 (real, member_detail): 'Savings Balance' is the accessible name of three distinct
    elements -- a cell, an iframe, and the generic node holding the value -- the exact collision
    docs/adr/0006 measures. A name-only search finds all three; role is what separates them, and
    resolving role=generic name='Savings Balance' is unique at ROLE_NAME_EXACT.
    """
    name_only_matches = [e for e in MEMBER_DETAIL.elements if e.name == "Savings Balance"]
    assert len(name_only_matches) == 3
    assert {e.role for e in name_only_matches} == {"cell", "iframe", "generic"}

    descriptor = TargetDescriptor(role="generic", name="Savings Balance")
    resolution = resolve(descriptor, MEMBER_DETAIL)

    assert resolution.element is not None
    assert resolution.element.role == "generic"
    assert resolution.strategy_used == ResolutionStrategy.ROLE_NAME_EXACT
    assert resolution.rank == 1


# --------------------------------------------------------------------------------------
# 7. A descriptor matching nothing returns element=None with every rung's candidate count.
# --------------------------------------------------------------------------------------


def test_no_match_returns_none_with_per_strategy_candidate_counts() -> None:
    """role="checkbox" is chosen because SUBACCOUNT_FORM has zero elements of that role, so
    ROLE_ORDINAL's pool is genuinely empty too. role="button" was tried first and rejected for
    this test: SUBACCOUNT_FORM has exactly one button ("Submit"), and a singleton role-filtered
    pool is now a real ROLE_ORDINAL match (see the corrected reasoning in
    locator._strategy_role_ordinal) rather than a true no-match case."""
    descriptor = TargetDescriptor(role="checkbox", name="Does Not Exist Anywhere")

    resolution = resolve(descriptor, SUBACCOUNT_FORM)

    assert resolution.element is None
    assert len(resolution.attempts) == len(ResolutionStrategy)
    for attempt in resolution.attempts:
        assert attempt.candidate_count >= 0
        assert attempt.skipped_reason is not None


# --------------------------------------------------------------------------------------
# 8. describe() on an unlabeled input in a table row produces a relational hint.
# --------------------------------------------------------------------------------------


def test_describe_unlabeled_nickname_field_produces_relational_hint() -> None:
    """The nickname textbox (real, subaccount_form) has no accessible name of its own --
    name_source is 'row_label', meaning Phase 3 derived 'Nickname' from the row's leading cell.
    describe() records that same row relationship independently, as a relational hint."""
    element = next(
        e for e in SUBACCOUNT_FORM.elements if e.role == "textbox" and e.name == "Nickname"
    )
    assert element.name_source == "row_label"

    descriptor = describe(element, SUBACCOUNT_FORM)

    assert descriptor.relational == RelationalHint(kind="row_label", label="Nickname")


# --------------------------------------------------------------------------------------
# 9. Strategy order, instrumented: DOM_FALLBACK is never consulted before every role-and-name
# rung has been attempted, proven from the runtime attempts list, not from reading the source.
# --------------------------------------------------------------------------------------


def test_strategy_order_is_instrumented_dom_fallback_always_attempted_last() -> None:
    """role="checkbox": SUBACCOUNT_FORM has zero elements of that role, so every rung (including
    ROLE_ORDINAL's now-singleton-wins case) genuinely fails and the walk reaches DOM_FALLBACK.
    role="button" was tried first and rejected: SUBACCOUNT_FORM has exactly one button, which
    ROLE_ORDINAL now resolves uniquely (see locator._strategy_role_ordinal), so the walk would
    stop at rank 5 and never reach DOM_FALLBACK -- not what this test is checking."""
    descriptor = TargetDescriptor(role="checkbox", name="Does Not Exist Anywhere")

    resolution = resolve(descriptor, SUBACCOUNT_FORM)

    attempted_order = [attempt.strategy for attempt in resolution.attempts]
    assert attempted_order == list(ResolutionStrategy)
    assert attempted_order[-1] == ResolutionStrategy.DOM_FALLBACK
    assert attempted_order.index(ResolutionStrategy.DOM_FALLBACK) == len(attempted_order) - 1


# --------------------------------------------------------------------------------------
# 10. TargetDescriptor round-trips through JSON losslessly, scope tuples and relational included.
# --------------------------------------------------------------------------------------


def test_target_descriptor_round_trips_through_json_losslessly() -> None:
    descriptor = TargetDescriptor(
        role="combobox",
        name="Account Type",
        name_match="exact",
        scope=[("iframe", "Savings Balance"), ("cell", "Row 1")],
        frame_path=["contentframe", "/member/12345/balance"],
        ordinal=2,
        relational=RelationalHint(label="Account Type"),
        dom_fallback=None,
        confidence=0.6,
        notes="derived from the row's leading cell",
    )

    round_tripped = TargetDescriptor.model_validate_json(descriptor.model_dump_json())

    assert round_tripped == descriptor
    assert all(isinstance(hint, tuple) for hint in round_tripped.scope)
    assert round_tripped.relational == descriptor.relational


# --------------------------------------------------------------------------------------
# 11. THE DRIFT CASE (fact D): the descriptor Phase 2 originally recorded for the login
# username field. Constructed explicitly rather than read from artifacts/ -- that directory is
# regenerated by every real discover run (a second successful run of the same goal text writes
# a new version rather than overwriting the old one, but the CONTENT of any given version is
# still whatever that run actually produced) and is not a frozen historical fixture. See
# docs/adr/0009's addendum and ARCHITECTURE.md's Phase 7 section for why these tests pin to a
# constructed input instead.
# --------------------------------------------------------------------------------------


def test_drift_the_recorded_empty_named_login_field_still_resolves_by_ordinal() -> None:
    """The descriptor Phase 2 originally recorded for the login username field: role=textbox,
    name="", ordinal=0 -- exactly how Phase 2 perceived it, before Phase 3 added name
    derivation. Phase 3 now perceives that same field as name="Username" (fact D,
    docs/adr/0004), which alone broke this recorded step when it was still live (verified at
    the time: five identical hard_failure results on step 0). This test has no browser and no
    live server, so the login page is stood in with a small CONSTRUCTED Observation shaped the
    way Phase 3+ perceives it today: no textbox is named "" any more.
    """
    recorded = TargetDescriptor(role="textbox", name="", ordinal=0)
    assert recorded.scope == []  # nothing else was ever recorded for this step
    assert recorded.relational is None

    login_page = Observation(
        url="http://127.0.0.1:5055/login",
        title="Legacy Bank Back Office",
        elements=[
            UIElement(node_id="0", role="cell", name="Username"),
            UIElement(node_id="1", role="textbox", name="Username", name_source="row_label"),
            UIElement(node_id="2", role="cell", name="Password"),
            UIElement(node_id="3", role="textbox", name="Password", name_source="row_label"),
            UIElement(node_id="4", role="button", name="Login"),
        ],
    )

    resolution = resolve(recorded, login_page)

    assert resolution.element is not None
    assert resolution.element.node_id == "1"
    assert resolution.element.name == "Username"
    assert resolution.strategy_used == ResolutionStrategy.ROLE_ORDINAL
    assert resolution.rank == 5

    by_strategy = {attempt.strategy: attempt for attempt in resolution.attempts}
    assert by_strategy[ResolutionStrategy.ROLE_NAME_EXACT].candidate_count == 0
    assert by_strategy[ResolutionStrategy.ROLE_NAME_NORMALIZED].candidate_count == 0
    assert by_strategy[ResolutionStrategy.ROLE_NAME_SCOPED].skipped_reason is not None
    assert by_strategy[ResolutionStrategy.RELATIONAL].skipped_reason is not None
    assert by_strategy[ResolutionStrategy.ROLE_ORDINAL].candidate_count == 1
    assert by_strategy[ResolutionStrategy.ROLE_ORDINAL].skipped_reason is None


# --------------------------------------------------------------------------------------
# 12. THE ROUND-2 REGRESSION: the descriptor Phase 2 originally recorded for step 3
# (ordinal=null, not ordinal=0 like step 0/1 above) resolves via ROLE_ORDINAL when its
# role-filtered pool is a singleton, even though no ordinal was ever recorded. A live
# end-to-end replay against the fixture server proved this case is real, not hypothetical, at
# the time: step 3's recorded name="" (Phase 2) perceives as name="Member ID" under Phase 3
# derivation, and the Member ID field is the only textbox on that page, so every name-based
# rung reports zero candidates and ROLE_ORDINAL is the first rung to see it. Constructed
# explicitly rather than read from artifacts/, for the same reason as case 11 above.
# --------------------------------------------------------------------------------------


def test_step3_real_descriptor_no_ordinal_resolves_via_role_ordinal_singleton() -> None:
    """The descriptor Phase 2 originally recorded for step 3: role=textbox, name="",
    ordinal=null -- the Member ID search field, as Phase 2 perceived it. Phase 3 now derives
    name="Member ID" for that same, unchanged field (fact D's exact mechanism, one step later in
    the same flow), so ROLE_NAME_EXACT and ROLE_NAME_NORMALIZED both see zero candidates. This
    observation is built by hand with exactly one textbox (the drifted-name field) so the case
    is isolated: a role-filtered pool of one is a unique match even with no ordinal recorded,
    because there is no second candidate to bet on positionally.
    """
    recorded = TargetDescriptor(role="textbox", name="", ordinal=None)

    search_page = Observation(
        url="http://127.0.0.1:5055/members",
        title="Legacy Bank Back Office",
        elements=[
            UIElement(node_id="0", role="cell", name="Member ID"),
            UIElement(node_id="1", role="textbox", name="Member ID", name_source="row_label"),
            UIElement(node_id="2", role="button", name="Search"),
        ],
    )

    resolution = resolve(recorded, search_page)

    assert resolution.element is not None
    assert resolution.element.node_id == "1"
    assert resolution.strategy_used == ResolutionStrategy.ROLE_ORDINAL
    assert resolution.rank == 5

    by_strategy = {attempt.strategy: attempt for attempt in resolution.attempts}
    assert by_strategy[ResolutionStrategy.ROLE_NAME_EXACT].candidate_count == 0
    assert by_strategy[ResolutionStrategy.ROLE_NAME_NORMALIZED].candidate_count == 0
    assert by_strategy[ResolutionStrategy.ROLE_ORDINAL].candidate_count == 1
    assert by_strategy[ResolutionStrategy.ROLE_ORDINAL].skipped_reason is None


# --------------------------------------------------------------------------------------
# 13. The fix must not turn ambiguity into first-match-wins: two role-matching candidates with no
# ordinal recorded is still skipped.
# --------------------------------------------------------------------------------------


def test_role_ordinal_with_two_candidates_and_no_ordinal_is_still_skipped_as_ambiguous() -> None:
    """CONSTRUCTED: two textboxes share a role with no ordinal recorded and no name match, so
    ROLE_ORDINAL must still report ambiguous and skip. A positional bet only exists when there is
    more than one candidate to choose among by index, and here there genuinely are two -- unlike
    the singleton pool in test 12 above, picking either one here would be a real guess.
    """
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[
            UIElement(node_id="0", role="textbox", name="Field A"),
            UIElement(node_id="1", role="textbox", name="Field B"),
        ],
    )
    descriptor = TargetDescriptor(role="textbox", name="")  # name drifted away from both

    resolution = resolve(descriptor, observation)

    assert resolution.element is None
    assert resolution.ambiguous is True

    by_strategy = {attempt.strategy: attempt for attempt in resolution.attempts}
    assert by_strategy[ResolutionStrategy.ROLE_ORDINAL].candidate_count == 2
    assert by_strategy[ResolutionStrategy.ROLE_ORDINAL].skipped_reason is not None


# --------------------------------------------------------------------------------------
# 14. ROUND 3, FIX 2: the singleton-wins rescue must never apply to a descriptor that recorded a
# MEANINGFUL name. A recorded name that matches nothing today is a genuine drift signal, and the
# only same-role survivor being rescued anyway would be a confident wrong action, not a recovery.
# --------------------------------------------------------------------------------------


def test_meaningful_recorded_name_that_matches_nothing_is_not_rescued_by_a_singleton_pool() -> None:
    """CONSTRUCTED: the page's only button is "Log out"; the descriptor recorded
    role=button, name="Confirm Transfer". The pre-fix singleton rule ("exactly one candidate
    remains, it wins") would resolve this to "Log out" at ROLE_ORDINAL and replay would click it
    -- the worst kind of wrong action, a confident one. Because the recorded name is non-empty,
    the rescue must not apply: element is None and the skipped_reason explains why.
    """
    observation = Observation(
        url="http://fake/",
        title="Fake",
        elements=[UIElement(node_id="0", role="button", name="Log out")],
    )
    descriptor = TargetDescriptor(role="button", name="Confirm Transfer")

    resolution = resolve(descriptor, observation)

    assert resolution.element is None

    by_strategy = {attempt.strategy: attempt for attempt in resolution.attempts}
    ordinal_attempt = by_strategy[ResolutionStrategy.ROLE_ORDINAL]
    assert ordinal_attempt.candidate_count == 1
    assert ordinal_attempt.skipped_reason is not None
    assert "Confirm Transfer" in ordinal_attempt.skipped_reason


# --------------------------------------------------------------------------------------
# 15. ROUND 3, FIX 4: replay/engine.py must not report success when the final checkpoint failed
# to verify. Exercised directly against the pure decision helper (no Surface, no browser) since
# replay() itself always launches a real WebSurface; the end-to-end path is covered live by the
# CLI replay command against the fixture server.
# --------------------------------------------------------------------------------------


def test_finish_result_is_hard_failure_when_checkpoint_did_not_verify() -> None:
    """Before this fix, replay() returned Success(checkpoint_verified=False) here, and cli.py
    only exits non-zero on hard_failure -- so a replay that never reached its goal printed a
    success result and exited 0. The checkpoint is the one place "done" is decided, not optimism.
    """
    checkpoint = Checkpoint(kind="text_present", target="page", value="Transfer Complete")

    result = _finish_result(
        checkpoint_verified=False, success_checkpoint=checkpoint, outputs={}, steps_run=4
    )

    assert result.kind == "hard_failure"
    assert result.step_id == 4
    assert "Transfer Complete" in result.expected
    assert "not present" in result.observed


def test_finish_result_is_success_when_checkpoint_verified() -> None:
    checkpoint = Checkpoint(kind="text_present", target="page", value="Transfer Complete")

    result = _finish_result(
        checkpoint_verified=True,
        success_checkpoint=checkpoint,
        outputs={"balance": "$100"},
        steps_run=4,
    )

    assert result.kind == "success"
    assert result.outputs == {"balance": "$100"}
    assert result.steps_run == 4
