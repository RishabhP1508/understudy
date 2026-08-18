"""TargetDescriptor and resolution: a ranked list of independent signals, walked in strict
priority order -- never a CSS selector, never a live ref. See
docs/adr/0006-ranked-descriptors-over-selectors.md for why: derived names collide by name alone
(the same caption ends up on a cell, an iframe, and the control it labels -- measured, three-way,
on the real fixture), and a name that matched at record time can stop matching after perception
itself improves with the app unchanged (fact D in the ADR: the login field went from
`role=textbox, name=""` to `role=textbox, name="Username"` with no app change at all).
Resolution therefore never trusts a single signal. It walks role+name, then a scoped variant of
the same, then a relational hint, then plain ordinal, then a CSS fallback this surface can never
actually satisfy, and requires a UNIQUE match at each rung before it stops. A rung matching more
than one element is recorded as ambiguous and skipped, never resolved to the first hit.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_serializer

from understudy.models.observation import STRUCTURAL_EXTRA, Observation, UIElement

# ADR 0004: a container has no caption of its own. Climbing through one to find a relational
# label would attach an unrelated outer row's caption to an inner control, so describe() never
# derives a relational hint for these roles -- the same restriction _derive_structural_names
# applies in surface/web.py, for the same reason.
_CONTAINER_ROLES = {"cell", "row", "rowgroup", "table"}


class RelationalHint(BaseModel):
    """The one relational kind this app needs: the control that sits in a table row whose
    leading (first, left-to-right) named cell reads `label`. Not a general taxonomy -- there is
    no second kind measured on this target.
    ponytail: add a `kind` value when a real target needs one, not speculatively.
    """

    kind: Literal["row_label"] = "row_label"
    # The captured neighbouring caption: the same kind of value as UIElement.name, so it is
    # structural in the same D4 (Phase 8) sense -- a label the app itself rendered, not a value.
    label: str = Field(json_schema_extra=STRUCTURAL_EXTRA)


class TargetDescriptor(BaseModel):
    """A recorded target: several independent signals, not one selector. Only `role` is
    required; everything else is evidence a resolver may or may not have (docs/adr/0006).
    Must round-trip losslessly through model_dump_json/model_validate_json, since this is exactly
    what gets written into and read back out of a Capability's steps.
    """

    role: str = Field(json_schema_extra=STRUCTURAL_EXTRA)
    name: str = Field(default="", json_schema_extra=STRUCTURAL_EXTRA)
    name_match: Literal["exact", "normalized", "regex"] = Field(
        default="exact", json_schema_extra=STRUCTURAL_EXTRA
    )
    scope: list[tuple[str, str]] = Field(
        default_factory=list, json_schema_extra=STRUCTURAL_EXTRA
    )  # ancestor hints, outermost first
    frame_path: list[str] = Field(default_factory=list, json_schema_extra=STRUCTURAL_EXTRA)
    ordinal: int | None = None  # tiebreaker only, never the primary signal
    relational: RelationalHint | None = None
    dom_fallback: str | None = Field(
        default=None, json_schema_extra=STRUCTURAL_EXTRA
    )  # CSS; explicitly brittle, attempted last, never fabricated
    confidence: float = 0.0
    notes: str = ""
    # STRUCTURAL: the 1-based rank (ResolutionStrategy walk order) of the strategy that resolved
    # this descriptor against the very observation it was captured from -- describe()'s own honest
    # self-check, computed by immediately resolving what it just built. None means "recorded
    # before this field existed" (every artifact already on disk); replay compares its own
    # actual_rank against this via drift_delta() as a signal, never a gate.
    recorded_rank: int | None = Field(default=None, json_schema_extra=STRUCTURAL_EXTRA)

    @field_serializer("confidence")
    def _round_confidence(self, value: float) -> float:
        """D6 (Phase 8): rounded only at SERIALIZATION, not at construction, so the computed
        value stays exact in memory (describe() below sums and clamps several small floats, and
        rounding that arithmetic early would just move the same float noise -- e.g.
        0.49999999999999994 -- one step earlier instead of removing it). Fixes the cosmetic but
        real cost measured in the real artifact: confidences serializing as
        0.49999999999999994 / 0.9500000000000001."""
        return round(value, 4)


class ResolutionStrategy(StrEnum):
    """Strict priority order (docs/adr/0006). Iterating this enum IS the walk order resolve()
    follows, so the order below is not just documentation -- it is what runs."""

    ROLE_NAME_EXACT = "role_name_exact"
    ROLE_NAME_NORMALIZED = "role_name_normalized"
    ROLE_NAME_SCOPED = "role_name_scoped"
    RELATIONAL = "relational"
    ROLE_ORDINAL = "role_ordinal"
    DOM_FALLBACK = "dom_fallback"


class StrategyAttempt(BaseModel):
    strategy: ResolutionStrategy
    candidate_count: int
    skipped_reason: str | None = None


class Resolution(BaseModel):
    element: UIElement | None
    strategy_used: ResolutionStrategy | None = None
    rank: int | None = None  # 1-based position of strategy_used in the walk order
    candidate_count: int
    ambiguous: bool
    attempts: list[StrategyAttempt]


class AmbiguousTarget(Exception):
    """Not raised by resolve() (docs/adr/0006: an ambiguous rung is skipped, not a hard error).
    Kept importable in case anything still catches it."""


class TargetNotFound(Exception):
    """Not raised by resolve(). A failed match is reported by returning element=None with the
    full per-strategy attempts list (see Resolution), not by raising."""


# --------------------------------------------------------------------------------------
# pure helpers: name comparison, and rebuilding tree structure from an Observation alone
# --------------------------------------------------------------------------------------


def _normalize(name: str) -> str:
    return " ".join(name.split()).casefold()


def _name_matches(candidate_name: str, target_name: str, mode: str) -> bool:
    if mode == "regex":
        return re.fullmatch(target_name, candidate_name) is not None
    if mode == "normalized":
        return _normalize(candidate_name) == _normalize(target_name)
    return candidate_name == target_name


def _is_subsequence(needle: list[tuple[str, str]], haystack: list[tuple[str, str]]) -> bool:
    """True if every item of `needle` appears in `haystack`, in the same relative order, not
    necessarily adjacent (a plain substring match would break the moment one intermediate
    ancestor changes)."""
    it = iter(haystack)
    return all(item in it for item in needle)


def _outermost_first_ancestors(element: UIElement) -> list[tuple[str, str]]:
    # element.ancestors is nearest-first and capped at 5 (surface/web.py); scope is recorded and
    # matched outermost-first, so this is the one place that ordering flips.
    return list(reversed(element.ancestors))


def _index_of(elements: list[UIElement], node_id: str) -> int:
    for index, element in enumerate(elements):
        if element.node_id == node_id:
            return index
    raise ValueError(f"node_id {node_id!r} is not present in this observation")


def _parents_from_depth(elements: list[UIElement]) -> list[int | None]:
    """Rebuild the parent-index array from `depth` alone. `elements` is a pre-order walk of the
    accessibility tree (a parent always precedes its children -- surface/web.py's parser
    guarantees that), so a stack keyed on depth reconstructs the tree exactly. This replays, on a
    plain Observation, the same technique _parse_snapshot uses while it still has live
    indentation -- necessary here because parent links are not part of the serialized schema.
    """
    parents: list[int | None] = []
    stack: list[tuple[int, int]] = []
    for index, element in enumerate(elements):
        while stack and stack[-1][0] >= element.depth:
            stack.pop()
        parents.append(stack[-1][1] if stack else None)
        stack.append((element.depth, index))
    return parents


def _children_map(parents: list[int | None]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for index, parent in enumerate(parents):
        if parent is not None:
            children.setdefault(parent, []).append(index)
    return children


def _subtree(elements: list[UIElement], root: int) -> list[int]:
    """Every descendant of `root`, by contiguity: a pre-order walk keeps a whole subtree in one
    unbroken run, ending at the first later element whose depth is not deeper than the root's."""
    root_depth = elements[root].depth
    result = []
    for index in range(root + 1, len(elements)):
        if elements[index].depth <= root_depth:
            break
        result.append(index)
    return result


def _containing_row_cell(
    elements: list[UIElement], parents: list[int | None], start: int
) -> int | None:
    """ADR 0004's climb, replayed against a resolved Observation: from `start`, walk up until the
    parent is a `cell` whose own parent is a `row`, crossing container boundaries (including an
    iframe) for free. `start` itself is never inspected, only its ancestors -- callers still
    restrict this to non-container roles (see _CONTAINER_ROLES) because a container has no
    caption of its own to be climbing away from.
    """
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


def _leading_named_cell(
    elements: list[UIElement], children: dict[int, list[int]], row: int
) -> UIElement | None:
    row_cells = [c for c in children.get(row, []) if elements[c].role == "cell"]
    return next((elements[c] for c in row_cells if elements[c].name), None)


def _rows_labelled(
    elements: list[UIElement], children: dict[int, list[int]], label: str
) -> list[int]:
    """Every row that has, among its own direct cell children, a cell named exactly `label`."""
    rows = []
    for index, element in enumerate(elements):
        if element.role != "row":
            continue
        cells = [c for c in children.get(index, []) if elements[c].role == "cell"]
        if any(elements[c].name == label for c in cells):
            rows.append(index)
    return rows


# --------------------------------------------------------------------------------------
# the six strategies, in enum order. Each returns (candidates, explicit_skip_reason).
# An explicit reason means the strategy could not even apply (a precondition on the
# descriptor was not met) and is used verbatim; otherwise resolve() derives the reason
# from how many candidates came back: none, one (a win), or more than one (ambiguous).
# --------------------------------------------------------------------------------------


def _strategy_role_name_exact(
    descriptor: TargetDescriptor, observation: Observation
) -> tuple[list[UIElement], str | None]:
    candidates = [
        e
        for e in observation.elements
        if e.role == descriptor.role
        and _name_matches(e.name, descriptor.name, descriptor.name_match)
    ]
    return candidates, None


def _strategy_role_name_normalized(
    descriptor: TargetDescriptor, observation: Observation
) -> tuple[list[UIElement], str | None]:
    target = _normalize(descriptor.name)
    candidates = [
        e
        for e in observation.elements
        if e.role == descriptor.role and _normalize(e.name) == target
    ]
    return candidates, None


def _strategy_role_name_scoped(
    descriptor: TargetDescriptor, observation: Observation
) -> tuple[list[UIElement], str | None]:
    if not descriptor.scope and not descriptor.frame_path:
        return [], "no scope or frame_path recorded on this descriptor"
    target = _normalize(descriptor.name)
    candidates = []
    for e in observation.elements:
        if e.role != descriptor.role or _normalize(e.name) != target:
            continue
        if descriptor.frame_path and e.frame_path != descriptor.frame_path:
            continue
        if descriptor.scope and not _is_subsequence(
            descriptor.scope, _outermost_first_ancestors(e)
        ):
            continue
        candidates.append(e)
    return candidates, None


def _strategy_relational(
    descriptor: TargetDescriptor, observation: Observation
) -> tuple[list[UIElement], str | None]:
    if descriptor.relational is None:
        return [], "no relational hint recorded on this descriptor"
    elements = observation.elements
    parents = _parents_from_depth(elements)
    children = _children_map(parents)
    rows = _rows_labelled(elements, children, descriptor.relational.label)
    candidates = [
        elements[index]
        for row in rows
        for index in _subtree(elements, row)
        if elements[index].role == descriptor.role
    ]
    return candidates, None


def _role_pool(
    role: str, scope: list[tuple[str, str]], frame_path: list[str], elements: list[UIElement]
) -> list[UIElement]:
    """The exact pool an ordinal indexes into: every element sharing `role`, narrowed by `scope`
    and `frame_path` if either is recorded -- role alone, never role+name. describe() and
    _strategy_role_ordinal both call this one helper so a recorded ordinal is guaranteed to mean
    the same position at replay time that it meant at record time. Before this helper existed the
    two computed the pool differently (describe() indexed the role+name pool, resolution indexed
    the role-only pool), so a recorded ordinal could silently select the wrong element -- e.g. an
    [Edit, Delete, Edit] row where recording "the second Edit" as ordinal=1 (its position among
    the two same-named Edit buttons) replayed as ordinal=1 in the all-buttons pool and clicked
    Delete instead.
    """
    pool = [e for e in elements if e.role == role]
    if scope:
        pool = [e for e in pool if _is_subsequence(scope, _outermost_first_ancestors(e))]
    if frame_path:
        pool = [e for e in pool if e.frame_path == frame_path]
    return pool


def _strategy_role_ordinal(
    descriptor: TargetDescriptor, observation: Observation
) -> tuple[list[UIElement], str | None]:
    pool = _role_pool(
        descriptor.role, descriptor.scope, descriptor.frame_path, observation.elements
    )
    if descriptor.ordinal is None:
        # A role-filtered pool of exactly one is not a positional bet -- a positional bet only
        # exists when there is more than one candidate to choose among by index -- so it is
        # rescued here, but ONLY when the descriptor never recorded a meaningful name to begin
        # with (descriptor.name == ""). An empty recorded name carried no information (Phase 2's
        # `role=textbox, name=""` login field is the real case this covers), so role plus position
        # is all that descriptor ever had. A descriptor that DID record a real name and now matches
        # nothing is a genuine drift signal instead: rescuing it off the sole same-role survivor
        # would be a confident wrong guess (e.g. `role=button, name="Confirm Transfer"` must never
        # resolve to the only button on the page just because that button happens to be
        # "Log out") -- so it falls through and fails, reported as a debuggable drift, not acted on.
        if len(pool) == 1 and descriptor.name == "":
            return pool, None
        if len(pool) == 1:
            return (
                pool,
                f"descriptor recorded name={descriptor.name!r}, which matched no element; the "
                f"sole remaining role={descriptor.role!r} candidate is not used to rescue a "
                "descriptor that once had a meaningful name",
            )
        return pool, f"no ordinal recorded; {len(pool)} candidate(s) share role={descriptor.role!r}"
    if not 0 <= descriptor.ordinal < len(pool):
        return (
            pool,
            f"ordinal {descriptor.ordinal} out of range for {len(pool)} "
            f"role={descriptor.role!r} candidate(s)",
        )
    return [pool[descriptor.ordinal]], None


def _strategy_dom_fallback(
    descriptor: TargetDescriptor, observation: Observation
) -> tuple[list[UIElement], str | None]:
    if descriptor.dom_fallback is None:
        return [], "no dom_fallback recorded on this descriptor"
    # There is no DOM in an accessibility Observation: a CSS selector is attempted (recorded
    # here) and can never be resolved against it. Never fabricate a match.
    return [], "this surface has no DOM; a CSS fallback cannot be evaluated against an Observation"


_STRATEGY_FUNCS = {
    ResolutionStrategy.ROLE_NAME_EXACT: _strategy_role_name_exact,
    ResolutionStrategy.ROLE_NAME_NORMALIZED: _strategy_role_name_normalized,
    ResolutionStrategy.ROLE_NAME_SCOPED: _strategy_role_name_scoped,
    ResolutionStrategy.RELATIONAL: _strategy_relational,
    ResolutionStrategy.ROLE_ORDINAL: _strategy_role_ordinal,
    ResolutionStrategy.DOM_FALLBACK: _strategy_dom_fallback,
}


def resolve(descriptor: TargetDescriptor, observation: Observation) -> Resolution:
    """Walk the six strategies in strict priority order. The first to match EXACTLY ONE element
    wins; a rung matching more than one is recorded ambiguous and skipped, never resolved to the
    first hit. If nothing ever resolves uniquely, element is None and `attempts` carries every
    rung's candidate count so the failure is debuggable (docs/adr/0006).
    """
    attempts: list[StrategyAttempt] = []
    for strategy in ResolutionStrategy:
        candidates, explicit_skip = _STRATEGY_FUNCS[strategy](descriptor, observation)
        count = len(candidates)
        if explicit_skip is not None:
            reason: str | None = explicit_skip
        elif count == 0:
            reason = "no candidates matched"
        elif count > 1:
            reason = f"{count} candidates matched; ambiguous"
        else:
            reason = None  # exactly one candidate, and the strategy applied cleanly: it wins
        attempts.append(
            StrategyAttempt(strategy=strategy, candidate_count=count, skipped_reason=reason)
        )
        if reason is None:
            return Resolution(
                element=candidates[0],
                strategy_used=strategy,
                rank=len(attempts),
                candidate_count=count,
                ambiguous=False,
                attempts=attempts,
            )
    ambiguous = any(attempt.candidate_count > 1 for attempt in attempts)
    return Resolution(
        element=None,
        strategy_used=None,
        rank=None,
        candidate_count=0,
        ambiguous=ambiguous,
        attempts=attempts,
    )


# --------------------------------------------------------------------------------------
# confidence: which signals were available when this descriptor was captured. Higher for an
# authored name than a derived one (ADR 0004: a derived name is an inference about layout, not
# a contract the app offers), lower again when a positional ordinal was needed at all, and
# nudged up when a relational hint is available as extra redundancy.
# --------------------------------------------------------------------------------------
_CONFIDENCE_BY_NAME_SOURCE = {
    "a11y": 0.9,
    "row_label": 0.6,
    "column_header": 0.55,
    "attr_name": 0.45,
    "none": 0.2,
}


def describe(element: UIElement, observation: Observation) -> TargetDescriptor:
    """The inverse of resolve(): capture every independent signal available for `element` right
    now, so a later resolve() call has more than one way to find it again (docs/adr/0006).
    """
    elements = observation.elements
    parents = _parents_from_depth(elements)
    children = _children_map(parents)
    index = _index_of(elements, element.node_id)

    named_ancestors_nearest_first = [a for a in element.ancestors if a[1]]
    scope = list(reversed(named_ancestors_nearest_first[:2]))
    frame_path = list(element.frame_path)

    # Fix (round 3): ordinal must be computed in EXACTLY the pool _strategy_role_ordinal indexes
    # at replay time -- role only, narrowed by this same scope/frame_path -- never the role+name
    # pool. See _role_pool's docstring for the wrong-element failure that drift caused.
    role_pool = _role_pool(element.role, scope, frame_path, elements)
    ordinal: int | None = None
    if len(role_pool) > 1:
        ordinal = next(i for i, e in enumerate(role_pool) if e.node_id == element.node_id)

    relational: RelationalHint | None = None
    if element.role not in _CONTAINER_ROLES:
        row_cell = _containing_row_cell(elements, parents, index)
        if row_cell is not None:
            row = parents[row_cell]
            assert row is not None  # _containing_row_cell only returns a cell whose parent is a row
            leading = _leading_named_cell(elements, children, row)
            if leading is not None:
                relational = RelationalHint(label=leading.name)

    confidence = _CONFIDENCE_BY_NAME_SOURCE.get(element.name_source, 0.2)
    if ordinal is not None:
        confidence -= 0.15
    if relational is not None:
        confidence += 0.05
    confidence = max(0.0, min(1.0, confidence))

    descriptor = TargetDescriptor(
        role=element.role,
        name=element.name,
        name_match="exact",
        scope=scope,
        frame_path=frame_path,
        ordinal=ordinal,
        relational=relational,
        dom_fallback=None,
        confidence=confidence,
        notes="no dom_fallback recorded: perception is accessibility-only and never reads the DOM",
    )
    # The only honest source for recorded_rank: resolve() is pure and cheap, and this is exactly
    # the same observation the descriptor was just captured from, so the rank it reports is what
    # replay is meant to reproduce later, not a guess.
    resolution = resolve(descriptor, observation)
    return descriptor.model_copy(update={"recorded_rank": resolution.rank})


def drift_delta(recorded_rank: int, actual_rank: int) -> int:
    """How many rungs weaker (positive) or stronger (negative) the strategy that actually
    resolved was, compared to the rank recorded at discovery time. Not wired anywhere in this
    phase -- Phase 9 reads it off replay's resolve() output as a drift signal.
    """
    return actual_rank - recorded_rank
