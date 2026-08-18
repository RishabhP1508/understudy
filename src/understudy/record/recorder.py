"""build_capability: a separate pass over a written run.jsonl, producing a Capability.

This reads the evidence log rather than any live object, so recording never depends on the
discovery process still being in memory, and the recorder never runs during the loop itself. The
pass has several stages, in order:

1. Filter to dispatched (`policy_decision.allowed is True`) `act` events, and drop the harness's
   own bootstrap navigate to the target (always the first allowed act event) -- it is represented
   by `target.entry_point`, never duplicated as a Step.
2. Prune exploratory dead ends: any subsequence that returns to a previously visited state without
   making progress is removed. A per-turn Observation snapshot is not yet part of the evidence
   format (Phase 8's own D2 change starts closing that gap for FUTURE logs by finally logging
   `observation_digest` on every act/decide event), so a log recorded before that change -- every
   log this project has today, including the one this phase records from -- has no digest to
   compare. This falls back to the sorted set of currently-loaded URLs
   (`policy_decision.checked_urls`) as a coarser stand-in state signature.
3. Merge consecutive redundant navigations (the later one supersedes the earlier).
4. Generalize the success checkpoint (`_generalize_success_checkpoint`, Phase 9) BEFORE
   postconditions are derived, because the last step's postcondition IS the success checkpoint
   (see stage 5): a `text_present` checkpoint whose value is exactly what an `extract` step in
   this same run produced is asserting THIS INVOCATION's OWN OUTPUT (member 12345's real balance),
   never a property any other invocation can be expected to reproduce, so it is rewritten to
   assert only that the extract step's own recorded role+name is present, with the invocation's
   data removed.
5. Build each Step's TargetDescriptor. `locator.describe()` is the general mechanism, but it needs
   a live Observation to run against, and this log carries none (a11y/ snapshots are written on
   FAILURE only) -- so the descriptor actually used is the one `describe()` ALREADY computed live,
   at discovery time (agent/loop.py), and serialized into the event's own `context.target`. This
   recorder parses that, rather than recomputing it; "the resolution rank achieved at record time"
   cannot be independently verified from this log format, for the same reason.
6. Derive each step's postcondition (see `_derive_postcondition`).
7. Promote each event's `rationale` into that step's rationale VERBATIM -- never regenerated,
   paraphrased, or tidied. It is the model's own stated reason, and it is what replay logs cite
   when no model is present.
8. Canonicalize routes and parameterize values (record/canonicalize.py) against the goal text.
9. Generalize a descriptor's own NAME and `frame_path` segments (`_parameterize_names_and_frames`,
   Phase 9) using the identical value->param map stage 8 already built: a goal literal can leak
   into an element's own accessible name (a member list row's link text embeds that member's own
   name, e.g. "12345 - Testuser Alpha") or into a frame's URL-shaped path segment, neither of which
   stage 8's route/value canonicalization reaches (those rewrite Checkpoint URLs and Step values,
   not a TargetDescriptor's own fields).
10. Seed `known_outcomes`/`recovery_rules`, each GATED rather than applied wholesale (Phase 9):
    see `_seed_known_outcomes`/`_seed_recovery_rules` for the two different gating axes.
11. One OPTIONAL, structured model call for name, description, and output descriptions (D5):
    degrades to a deterministic name/description derived from the goal string with no network at
    all if no LLMClient is given, the call raises, or its answer names an output no step extracts
    (rejected and retried once).
12. Compute `transcript_hash` from the run's own `transcript.jsonl` (the actual raw model
    transcript this project promises never to store) -- never `run.jsonl`, which is the recorded
    EVENT log, a different file with a different purpose. `stability` stays None; Phase 9's own
    five-run replay check is what writes it.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from understudy.llm.base import LLMClient
from understudy.models.artifact import (
    Capability,
    Checkpoint,
    InputParam,
    KnownOutcome,
    OutputField,
    ParamRef,
    Provenance,
    RecoveryRule,
    Step,
    TargetApp,
    login_prefix_len,
)
from understudy.models.observation import PERCEPTION_VERSION
from understudy.record.canonicalize import (
    canonicalize_route,
    goal_literals,
    infer_param_name,
    infer_type,
)
from understudy.safety.policy import Policy
from understudy.safety.redact import classify_field_sensitivity
from understudy.surface.locator import TargetDescriptor

# Pre-existing placeholders: PolicyGate already substitutes these, at discovery time, for any Type
# action whose target element's sensitivity is "secret" or "pii" (safety/policy.py's _log). The
# recorder never sees the real value in either case -- it was never written to disk. The two use
# different prefixes ("param" vs "pii") specifically so the recorder can recover WHICH sensitivity
# produced a given placeholder from its text alone, without re-guessing from the field name (which
# can disagree with the live element's own policy.sensitive_fields match -- see docs/adr/0013).
_EXISTING_SECRET_REF_RE = re.compile(r"^\$\{param:([a-z0-9\-]+)\}$")
_EXISTING_PII_REF_RE = re.compile(r"^\$\{pii:([a-z0-9\-]+)\}$")

# Seeding known_outcomes and recovery_rules is gated, not applied wholesale -- on two DIFFERENT
# axes, because they answer two different questions:
#
# A KNOWN OUTCOME is gated on WHAT THE FLOW CAN PRODUCE. Before this gating, every capability
# recorded declared `insufficient_funds` -- an outcome a read-only balance lookup can never
# possibly produce, a false contract in exactly the file the brief treats as a focal point, and a
# dead detector sitting in replay's hot path. `_seed_known_outcomes` below only adds an outcome
# when the recorded flow itself gives evidence it is reachable (a lookup control, a protected
# record's URL, typed input that gets submitted).
#
# A RECOVERY RULE is gated on WHETHER REPLAY CAN ACTUALLY PERFORM IT for this flow. A slow load, an
# unexpected dialog, or a transient 503 can happen to ANY flow regardless of what this particular
# recording did -- gating those on "did this run hit one" would strip every recovery rule from
# every clean recording, which is the opposite of useful (recovery rules exist precisely for
# conditions THIS run did not happen to hit). What genuinely CAN be absent is the ABILITY to
# recover: `reauth` means "re-navigate to the entry point and re-run the recorded login steps",
# which only makes sense when there IS a login prefix to re-run (`login_prefix_len` > 0). The first
# four rules below are therefore seeded unconditionally; only `reauth_on_session_expiry` is gated.
#
# `insufficient_funds` (detector `balance_check`) is seeded on the same PRODUCE axis as the other
# three, not dropped: `_earns_insufficient_funds` earns it when a flow types into a field whose
# name reads as monetary ("deposit", "amount", "transfer") and later submits it with a click -- the
# same "typed input that gets submitted" shape as `validation_rejected`, narrowed to a field that
# actually spends value. A read-only lookup still cannot earn it, for the same reason it cannot
# earn `validation_rejected`: it never types into anything. Phase 9 dropped this seed entirely
# rather than merely gating it, on the reasoning that no flow in the project could ever reach it
# and B1 (replay/outcomes.py) defined no `balance_check` detector for it to name -- correct at the
# time; a seed whose detector name does not resolve fails outcomes.validate() the moment any
# capability emits it, so an unused seed would have been a landmine. Phase 10 then recorded a real
# subaccount-opening capability whose flow types an amount into "Initial Deposit" and submits it --
# a flow that spends value and can be told "no" -- so Phase 11 wrote the detector, made the fixture
# (fixtures/legacy_bank/app.py) able to say no, and reinstated the seed. See docs/adr/0016.
_RECOVERY_RULE_SEEDS: tuple[RecoveryRule, ...] = (
    RecoveryRule(
        id="dismiss_native_dialog",
        trigger="native_dialog_appeared",
        action="dismiss_dialog",
        max_attempts=3,
    ),
    RecoveryRule(
        id="dismiss_html_interstitial",
        trigger="html_interstitial_present",
        action="dismiss",
        max_attempts=4,
    ),
    RecoveryRule(
        id="retry_transient_failure",
        trigger="transient_error_page",
        action="retry",
        max_attempts=5,
    ),
    RecoveryRule(
        id="wait_for_slow_load",
        trigger="navigation_still_in_flight",
        action="wait",
        max_attempts=3,
    ),
)


def _earns_member_not_found(steps: list[Step]) -> bool:
    """`member_not_found` can only ever fire on a flow that actually performs a record lookup --
    earned when some step's target name looks like a lookup field or control (case-insensitive
    "member id" or "search")."""
    for step in steps:
        if step.target is None:
            continue
        name = step.target.name.lower()
        if "member id" in name or "search" in name:
            return True
    return False


def _earns_permission_denied(recorded_urls: list[str]) -> bool:
    """`permission_denied` can only ever fire on a flow that opens a protected record -- earned
    when a recorded URL's path contains "/member/" (the fixture's 403 is only ever returned by
    /member/<id> and /member/<id>/balance)."""
    return any("/member/" in urlsplit(url).path for url in recorded_urls)


def _earns_validation_rejected(steps: list[Step]) -> bool:
    """`validation_rejected` can only ever fire on a flow that submits typed input -- earned when a
    `type` step is followed, later (not necessarily immediately), by a `click` step."""
    saw_type = False
    for step in steps:
        if step.action == "type":
            saw_type = True
        elif step.action == "click" and saw_type:
            return True
    return False


def _earns_insufficient_funds(steps: list[Step]) -> bool:
    """`insufficient_funds` can only ever fire on a flow that actually SPENDS value -- earned when
    a `type` step's target name, lowercased, reads as a monetary field ("deposit", "amount", or
    "transfer") and a later (not necessarily immediate) `click` step submits it. Mirrors
    `_earns_validation_rejected`'s shape: typed input that is never submitted cannot be rejected by
    the app at all, whether the rejection is a validation error or an insufficient-funds one."""
    saw_money_field = False
    for step in steps:
        if step.action == "type" and step.target is not None:
            name = step.target.name.lower()
            if "deposit" in name or "amount" in name or "transfer" in name:
                saw_money_field = True
        elif step.action == "click" and saw_money_field:
            return True
    return False


def _recorded_urls(steps: list[Step], success: Checkpoint) -> list[str]:
    """Every URL this recording actually touched, as far as the built Steps/Checkpoints can say:
    each step's own `url_matches` postcondition value, plus the success checkpoint's, if it is one
    too. Used only to decide which known_outcomes are earned (`_earns_permission_denied`), never
    serialized itself."""
    urls = [
        step.postcondition.value
        for step in steps
        if step.postcondition is not None and step.postcondition.kind == "url_matches"
    ]
    if success.kind == "url_matches":
        urls.append(success.value)
    return urls


def _seed_known_outcomes(steps: list[Step], recorded_urls: list[str]) -> list[KnownOutcome]:
    outcomes: list[KnownOutcome] = []
    if _earns_member_not_found(steps):
        outcomes.append(
            KnownOutcome(
                code="member_not_found",
                detector="member_lookup_no_match",
                terminal=True,
                message_template="No member found for the given id.",
            )
        )
    if _earns_permission_denied(recorded_urls):
        outcomes.append(
            KnownOutcome(
                code="permission_denied",
                detector="permission_denied",
                terminal=True,
                message_template="You do not have permission to view this record.",
            )
        )
    if _earns_validation_rejected(steps):
        outcomes.append(
            KnownOutcome(
                code="validation_rejected",
                detector="validation_rejected",
                terminal=True,
                message_template="The submitted value could not be validated.",
            )
        )
    if _earns_insufficient_funds(steps):
        outcomes.append(
            KnownOutcome(
                code="insufficient_funds",
                detector="balance_check",
                terminal=True,
                message_template="The submitted deposit exceeds the available balance.",
            )
        )
    return outcomes


def _seed_recovery_rules(steps: list[Step], target: str) -> list[RecoveryRule]:
    rules = list(_RECOVERY_RULE_SEEDS)
    if login_prefix_len(steps, target) > 0:
        rules.append(
            RecoveryRule(
                id="reauth_on_session_expiry",
                trigger="session_lost_mid_flow",
                action="reauth",
                max_attempts=1,
            )
        )
    return rules

_METADATA_SYSTEM_PROMPT = (
    "You name and describe an already-recorded, already-successful browser automation "
    "capability for a human reviewer and a calling agent. Propose a short name, a one-sentence "
    "description, and a one-sentence description for each named output. Never invent an output "
    "name that is not in the given list."
)

_METADATA_TOOL: dict[str, Any] = {
    "name": "propose_capability_metadata",
    "description": "Propose a name, description, and per-output descriptions for a capability.",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "A short, human-readable capability name."},
            "description": {
                "type": "string",
                "description": "One sentence describing what this capability does.",
            },
            "output_descriptions": {
                "type": "object",
                "description": "Map of output name to a one-sentence description of that output.",
                "additionalProperties": {"type": "string"},
            },
            "rationale": {
                "type": "string",
                "description": "Why this name and description fit the recorded steps.",
            },
        },
        "required": ["name", "description", "output_descriptions", "rationale"],
    },
}


# --------------------------------------------------------------------------------------
# stages 1-3: filter, prune, merge
# --------------------------------------------------------------------------------------


def _dispatched_act_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ALLOWED `act` event, except the harness's own bootstrap navigate (the first one)."""
    result: list[dict[str, Any]] = []
    first_seen = False
    for event in events:
        if event.get("type") != "act":
            continue
        decision = event.get("policy_decision") or {}
        if decision.get("allowed") is not True:
            continue
        if not first_seen:
            first_seen = True
            continue
        result.append(event)
    return result


def _progress_signature(event: dict[str, Any]) -> str:
    """A per-event state signature: the real `observation_digest` when a log carries one (D2,
    future logs), else the sorted set of every URL loaded at the time this event was dispatched
    (`policy_decision.checked_urls`) -- coarser (two different pages under the same URL look
    identical), but it is the only per-event state signal every log up to and including this
    phase's own recording actually carries.
    """
    digest = event.get("observation_digest")
    if digest:
        return f"digest:{digest}"
    decision = event.get("policy_decision") or {}
    checked = tuple(sorted(decision.get("checked_urls") or []))
    return f"urls:{checked}"


def _prune_dead_ends(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the DETOUR: a run of actions that started at a state already seen earlier and
    returned to it, netting zero progress.

    Only a NON-ADJACENT repeat of a state signature is a detour -- something ran in between and
    came back. An ADJACENT repeat (the very next event dispatched from the identical signature,
    e.g. a login form's username then password, both while the URL is still the login page) is
    ordinary sequential progress, never a detour, and must survive intact.

    When signature S is seen again at position `index`, having last been seen at position
    `earlier`, and `index > earlier + 1` (i.e. NOT adjacent -- real actions ran in between and
    came back), every event from `earlier` UP TO AND INCLUDING `index - 1` is the detour and is
    dropped -- this drops `earlier` itself too, not just what came after it, because `earlier` is
    precisely the action whose own effect is what started the excursion away from S in the first
    place (its result, not S, is what the very next event actually saw). The event AT `index` is
    the fresh decision made once back at S and is always kept, along with everything strictly
    before `earlier`.

    Round 1 of this function grouped consecutive same-signature events into a "run" first and
    pruned across runs, never within one. Measured directly against a synthetic `[A, A, B, A, C]`
    sequence, that version kept BOTH occurrences of the leading `A` run and dropped the returning
    `A` instead -- exactly backwards: it kept an action whose own effect transitions away to `B`
    with no matching return-trip action to undo it, and dropped the action that actually escapes
    the `A`/`B` loop toward `C`. A replayed artifact built that way has a hole no capability can
    bridge. This per-event version tracks the MOST RECENT prior position of each signature
    directly (no separate grouping pass), which is what correctly keeps a leading adjacent run
    intact while still erasing only the genuine detour that follows it.
    """
    last_seen: dict[str, int] = {}
    erase = [False] * len(events)
    for index, event in enumerate(events):
        signature = _progress_signature(event)
        if signature in last_seen:
            earlier = last_seen[signature]
            if index > earlier + 1:  # non-adjacent: a real detour ran in between
                for detour_index in range(earlier, index):
                    erase[detour_index] = True
        last_seen[signature] = index
    return [event for index, event in enumerate(events) if not erase[index]]


def _merge_consecutive_navigations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two or more `navigate` actions back to back with nothing in between: only the LAST one (the
    final destination actually reached) is kept."""
    merged: list[dict[str, Any]] = []
    for event in events:
        action = event.get("proposed_action") or {}
        previous_action = (merged[-1].get("proposed_action") or {}) if merged else {}
        if action.get("kind") == "navigate" and previous_action.get("kind") == "navigate":
            merged[-1] = event
            continue
        merged.append(event)
    return merged


# --------------------------------------------------------------------------------------
# stage 4: generalize the success checkpoint, if it is really this run's own output (Phase 9)
# --------------------------------------------------------------------------------------


def _generalize_success_checkpoint(
    success_checkpoint: Checkpoint, act_events: list[dict[str, Any]]
) -> Checkpoint:
    """The recorded success checkpoint is the model's own `finish` checkpoint -- often literally
    `text_present("$1,204.55")`, member 12345's real balance from THIS run. Replaying with a
    different member's id resolves every locator correctly and still fails this checkpoint, because
    it is asserting THIS INVOCATION's OWN OUTPUT, not a property the capability's CONTRACT actually
    makes -- the same defect class as a literal leaking into a route or a descriptor name, arriving
    through a third door.

    Fix: if the checkpoint is `text_present` and its value equals the `act_result` of some
    `extract` act event in this run, it is asserting exactly what that extract step already
    extracted. Replace it with `element_present` against THAT step's own recorded target
    role+name -- the same assertion ("the thing this capability looks up is present"), with the
    invocation-specific data removed. If no extract step produced that value, the checkpoint is
    left untouched: it is already invocation-independent (e.g. a fixed confirmation banner).

    Runs BEFORE `_attach_postconditions` (not after): the LAST step's postcondition IS this
    checkpoint (`_derive_postcondition`'s branch (c)), so generalizing it here, first, generalizes
    that postcondition too, in one place, rather than needing a second fix after the fact.
    """
    if success_checkpoint.kind != "text_present":
        return success_checkpoint
    for event in act_events:
        context = event.get("context") or {}
        if context.get("tool") != "extract":
            continue
        if (event.get("act_result") or "") == success_checkpoint.value:
            target = context.get("target") or {}
            return Checkpoint(
                kind="element_present",
                target=target.get("role", ""),
                value=target.get("name", ""),
            )
    return success_checkpoint


# --------------------------------------------------------------------------------------
# stages 5 and 7: build Steps (TargetDescriptor; rationale is promoted verbatim inline below)
# --------------------------------------------------------------------------------------


def _step_value(event: dict[str, Any], is_extract: bool) -> Any:
    action = event.get("proposed_action") or {}
    context = event.get("context") or {}
    if is_extract:
        return context.get("output_name")
    if action.get("kind") == "navigate":
        return action.get("url")
    if "text" in action:
        return action.get("text")
    return action.get("value")


def _build_steps(act_events: list[dict[str, Any]]) -> tuple[list[Step], list[OutputField]]:
    steps: list[Step] = []
    outputs: list[OutputField] = []
    for index, event in enumerate(act_events):
        action = event.get("proposed_action") or {}
        context = event.get("context") or {}
        tool = context.get("tool")
        is_extract = tool == "extract"
        action_kind = "extract" if is_extract else action.get("kind", "")

        target: TargetDescriptor | None = None
        if context.get("target") is not None:
            target = TargetDescriptor(**context["target"])

        risk_class = (event.get("policy_decision") or {}).get("risk", "SAFE_REVERSIBLE")
        step_id = str(index)

        steps.append(
            Step(
                id=step_id,
                index=index,
                action=action_kind,
                target=target,
                value=_step_value(event, is_extract),
                precondition=None,
                postcondition=None,  # filled in by _attach_postconditions
                risk_class=risk_class,
                rationale=event.get("rationale") or "",  # D1 step 6: promoted VERBATIM
                on_failure=None,
            )
        )
        if is_extract and context.get("output_name"):
            outputs.append(
                OutputField(
                    name=context["output_name"],
                    type="string",
                    description=event.get("rationale") or "",
                    source_step_id=step_id,
                )
            )
    return steps, outputs


# --------------------------------------------------------------------------------------
# stage 6: postcondition derivation (D1)
# --------------------------------------------------------------------------------------


def _checked_urls(event: dict[str, Any]) -> list[str]:
    decision = event.get("policy_decision") or {}
    return list(decision.get("checked_urls") or [])


def _derive_postcondition(
    index: int, act_events: list[dict[str, Any]], success_checkpoint: Checkpoint
) -> Checkpoint:
    """D1's precedence, applied per step:

    (c) the LAST step has no next event to derive a produced-state from at all; its postcondition
        is the run's own success checkpoint.
    (a) otherwise, if the URL state the NEXT event observed differs from this event's own, the
        postcondition is `url_matches` against the deepest (most nested child-frame) entry of
        the new state.
    (b) otherwise, for a read/extract step, `text_present` with the value it actually extracted.
    (d) otherwise (the URL state did not change), `element_present` against the NEXT step's own
        recorded target role+name -- the page reached a state where the next action's target
        actually exists.
    """
    if index == len(act_events) - 1:
        return success_checkpoint  # (c)

    event = act_events[index]
    next_event = act_events[index + 1]
    current_urls = _checked_urls(event)
    # LATENT, VERIFIED, NOT YET FIXED (docs/adr/0013): `checked_urls` does not mean the same
    # thing for every action kind (safety/policy.py's PolicyGate.dispatch). For a `navigate`
    # action it is `[action.url]` -- the DESTINATION, recorded before that navigate has actually
    # run -- and for every other action kind it is the URLs CURRENTLY loaded. If `next_event`
    # itself is a `navigate` step, `next_urls` below is therefore that navigate's own upcoming
    # destination, not "the state THIS step's action produced" -- a postcondition derived from it
    # would assert a URL that has not loaded yet at the point this step's postcondition is
    # actually checked (right after THIS step, before `next_event` has run at all). No capability
    # recorded so far has an interior `navigate` step (the only one, the harness's own bootstrap,
    # is excluded before this function ever runs), so this has never fired. Do not derive a
    # postcondition here assuming `next_event` is not itself a `navigate` step without addressing
    # this first.
    next_urls = _checked_urls(next_event)

    if next_urls and next_urls != current_urls:  # (a)
        return Checkpoint(kind="url_matches", target="any_frame", value=next_urls[-1])

    context = event.get("context") or {}
    if context.get("tool") == "extract":  # (b)
        extracted = event.get("act_result") or ""
        return Checkpoint(kind="text_present", target="page", value=extracted)

    next_context = next_event.get("context") or {}
    next_target = next_context.get("target") or {}
    return Checkpoint(  # (d)
        kind="element_present",
        target=next_target.get("role", ""),
        value=next_target.get("name", ""),
    )


def _attach_postconditions(
    steps: list[Step], act_events: list[dict[str, Any]], success_checkpoint: Checkpoint
) -> list[Step]:
    return [
        step.model_copy(
            update={"postcondition": _derive_postcondition(index, act_events, success_checkpoint)}
        )
        for index, step in enumerate(steps)
    ]


# --------------------------------------------------------------------------------------
# stage 8: canonicalize and parameterize
# --------------------------------------------------------------------------------------


def _typed_example(value: str, declared_type: str) -> str | int | float:
    """`InputParam.example` must agree with `InputParam.type`: an `example="12345"` (a str)
    alongside `type="integer"` contradicts its own declared type, which is exactly the contract
    Phase 11's `json_schema()` export hands to a calling agent. Cast the observed literal to match
    what `infer_type` already decided it is."""
    if declared_type == "integer":
        return int(value)
    if declared_type == "number":
        return float(value)
    return value


def _parameterize(
    steps: list[Step], goal: str
) -> tuple[list[Step], dict[str, InputParam], dict[str, str]]:
    """Two independent mechanisms produce a `ParamRef`, in this order per step:

    1. The step's own value is ALREADY a "${param:<slug>}" placeholder -- PolicyGate substituted
       it at discovery time because the target element's sensitivity was "secret" or "pii". The
       real value was never written to disk; this recorder only ever sees the placeholder.
    2. The step's own value, as a bare literal, matches a run of digits in the goal text
       (record/canonicalize.py's `goal_literals`) -- the same literal becomes a named parameter
       wherever it recurs, keyed by its FIELD name (e.g. "Member ID" -> "member_id").

    A param the recorder itself marks "secret" or "pii" never carries the observed literal in
    `example`: a field marked sensitive whose example is the real value is self-defeating.

    Returns the steps with their `value` replaced by a `ParamRef` where applicable, the
    `InputParam`s this produced (insertion order, keyed by name), and a value -> param_name map
    for route canonicalization to reuse against Checkpoints.
    """
    goal_lits = goal_literals(goal)
    inputs: dict[str, InputParam] = {}
    value_to_param: dict[str, str] = {}
    new_steps: list[Step] = []

    for index, step in enumerate(steps):
        value = step.value
        if not isinstance(value, str):
            new_steps.append(step)
            continue

        existing_secret_ref = _EXISTING_SECRET_REF_RE.fullmatch(value)
        existing_pii_ref = _EXISTING_PII_REF_RE.fullmatch(value)
        if existing_secret_ref is not None or existing_pii_ref is not None:
            existing_sensitivity: Literal["secret", "pii"] = (
                "secret" if existing_secret_ref is not None else "pii"
            )
            match = existing_secret_ref or existing_pii_ref
            assert match is not None  # one of the two, by the `if` above
            param_name = match.group(1)
            if param_name not in inputs:
                target_name = step.target.name if step.target else ""
                inputs[param_name] = InputParam(
                    name=param_name,
                    type="string",
                    required=True,
                    description=(
                        f"the value typed into {target_name!r}"
                        if target_name
                        else f"a {existing_sensitivity} input"
                    ),
                    example=None,  # never recoverable: the real value was redacted before logging
                    sensitivity=existing_sensitivity,
                )
            new_steps.append(step.model_copy(update={"value": ParamRef(name=param_name)}))
            continue

        if value in goal_lits:
            target_name = step.target.name if step.target else ""
            param_name = infer_param_name(target_name, fallback=f"param_{index}")
            if param_name not in inputs:
                sensitivity = classify_field_sensitivity(param_name)
                inputs[param_name] = InputParam(
                    name=param_name,
                    type=infer_type(value),
                    required=True,
                    description=(
                        f"the value typed into {target_name!r}" if target_name else ""
                    ),
                    # FIX 3: a param the recorder has just labelled sensitive never gets the
                    # observed literal as its example -- omitted, not stored as a shape hint,
                    # since a shape hint is more code than this project's own recorder needs
                    # to justify for a field no goal literal path has produced sensitively yet.
                    # B7: cast to match `infer_type(value)` -- an integer/number param's example
                    # must not be a bare str, which contradicts its own declared type.
                    example=(
                        _typed_example(value, infer_type(value)) if sensitivity == "none" else None
                    ),
                    sensitivity=sensitivity,
                )
            value_to_param[value] = param_name
            new_steps.append(step.model_copy(update={"value": ParamRef(name=param_name)}))
            continue

        new_steps.append(step)

    return new_steps, inputs, value_to_param


def _canonicalize_checkpoint(checkpoint: Checkpoint, value_to_param: dict[str, str]) -> Checkpoint:
    if checkpoint.kind != "url_matches" or not value_to_param:
        return checkpoint
    new_value = checkpoint.value
    for literal, param_name in value_to_param.items():
        new_value = canonicalize_route(new_value, literal, param_name)
    if new_value == checkpoint.value:
        return checkpoint
    return checkpoint.model_copy(update={"value": new_value})


def _canonicalize_steps(steps: list[Step], value_to_param: dict[str, str]) -> list[Step]:
    result = []
    for step in steps:
        if step.postcondition is None:
            result.append(step)
            continue
        canonicalized = _canonicalize_checkpoint(step.postcondition, value_to_param)
        result.append(step.model_copy(update={"postcondition": canonicalized}))
    return result


# --------------------------------------------------------------------------------------
# stage 9: a descriptor's own name and frame_path can ALSO embed a goal literal (Phase 9)
# --------------------------------------------------------------------------------------


def _regexify_name(name: str, literal: str, param_name: str) -> str:
    """Split `name` on every occurrence of `literal`; the parameter placeholder is anchored where
    the literal was, and every OTHER run of recorded text -- a leading, trailing, or in-between
    chunk -- becomes `.*`, never kept as literal text to match against. The recorder has no
    evidence that surrounding text is stable across a different parameter value (it is this
    recording's own observed data, e.g. a member's name attached to a member-list row), and
    resolution requires a UNIQUE match, so an over-broad pattern fails loudly rather than silently
    clicking the wrong row. "12345 - Testuser Alpha" with literal "12345" -> ":member_id.*".
    """
    segments = name.split(literal)
    pieces: list[str] = []
    for index, segment in enumerate(segments):
        if segment:
            pieces.append(".*")
        if index < len(segments) - 1:
            pieces.append(f":{param_name}")
    return "".join(pieces)


def _parameterize_target(
    target: TargetDescriptor, value_to_param: dict[str, str]
) -> TargetDescriptor:
    """Rewrite ONE descriptor's `frame_path` segments and `name`, using the SAME value->param map
    `_parameterize` already built -- see this module's stage-9 docstring for why both leak a goal
    literal that route/value canonicalization does not reach.

    (a) frame_path: each segment goes through the EXISTING `canonicalize_route` (never a second
        rewriter). A segment can be a bare frame name ("contentframe", untouched, no literal to
        replace) or a frame's own URL PATH ("/member/12345/balance") -- `canonicalize_route` treats
        either correctly since it only ever replaces a WHOLE path segment.
    (b) name: when a goal literal appears INSIDE the name, either the name IS exactly the literal
        (becomes ":param_name", `name_match` stays "exact") or the literal is EMBEDDED (becomes a
        regex via `_regexify_name`, `name_match` becomes "regex", and `notes` records why -- R2's
        robustness reasoning).
    """
    new_frame_path = list(target.frame_path)
    for index, segment in enumerate(new_frame_path):
        for literal, param_name in value_to_param.items():
            segment = canonicalize_route(segment, literal, param_name)
        new_frame_path[index] = segment

    original_name = target.name
    matched_literal = next(
        (literal for literal in value_to_param if literal and literal in original_name), None
    )
    if matched_literal is None:
        new_name = original_name
        new_name_match = target.name_match
        new_notes = target.notes
    else:
        param_name = value_to_param[matched_literal]
        if original_name == matched_literal:
            new_name = f":{param_name}"
            new_name_match = target.name_match  # stays "exact": the whole name IS the parameter
            new_notes = target.notes
        else:
            new_name = _regexify_name(original_name, matched_literal, param_name)
            new_name_match = "regex"
            note = (
                f"name generalized to a regex: the text around :{param_name} was this "
                "recording's own observed data (e.g. this record's own name), the recorder has "
                "no evidence it is stable across a different parameter value, and resolution "
                "requires a unique match, so an over-broad pattern fails loudly rather than "
                "silently resolving to the wrong element."
            )
            new_notes = f"{target.notes} {note}" if target.notes else note

    if (
        new_frame_path == target.frame_path
        and new_name == target.name
        and new_name_match == target.name_match
    ):
        return target
    return target.model_copy(
        update={"frame_path": new_frame_path, "name": new_name, "name_match": new_name_match,
                "notes": new_notes}
    )


def _parameterize_names_and_frames(
    steps: list[Step], value_to_param: dict[str, str]
) -> list[Step]:
    if not value_to_param:
        return steps
    result: list[Step] = []
    for step in steps:
        if step.target is None:
            result.append(step)
            continue
        new_target = _parameterize_target(step.target, value_to_param)
        if new_target is step.target:
            result.append(step)
        else:
            result.append(step.model_copy(update={"target": new_target}))
    return result


# --------------------------------------------------------------------------------------
# stage 11: the one optional structured model call (D5)
# --------------------------------------------------------------------------------------


def _propose_metadata(
    llm: LLMClient, goal: str, steps: list[Step], output_names: list[str]
) -> tuple[str, str, dict[str, str]] | None:
    """Degrades to None -- no name/description proposal at all -- on ANY of: no LLMClient given
    (checked by the caller before this is even invoked), the call raising, a malformed response,
    or a response that names an output no step actually extracts (rejected and retried ONCE, then
    given up on). Never the only path to a usable Capability: the caller falls back to a
    deterministic name/description derived from the goal string with no network at all.
    """
    step_summary = "\n".join(
        f"- {step.action}"
        + (f" (target: {step.target.name!r})" if step.target and step.target.name else "")
        for step in steps
    )
    prompt = (
        f"Goal: {goal}\n\nRecorded steps:\n{step_summary}\n\n"
        f"Known output names: {output_names or 'none'}"
    )
    messages: list[dict[str, Any]] = [{"role": "user", "text": prompt}]

    for _attempt in range(2):  # one call, one retry
        try:
            response = llm.complete(
                system=_METADATA_SYSTEM_PROMPT, messages=messages, tools=[_METADATA_TOOL]
            )
        except Exception:
            return None
        if not response.tool_calls:
            return None
        call = response.tool_calls[0]
        args = call.args
        name = args.get("name")
        description = args.get("description")
        output_descriptions = args.get("output_descriptions")
        if (
            not isinstance(name, str)
            or not isinstance(description, str)
            or not isinstance(output_descriptions, dict)
        ):
            return None
        unknown = set(output_descriptions) - set(output_names)
        if not unknown:
            cleaned = {k: v for k, v in output_descriptions.items() if isinstance(v, str)}
            return name, description, cleaned
        messages.append({"role": "model", "tool_calls": [{"name": call.name, "args": args}]})
        messages.append(
            {
                "role": "tool",
                "name": call.name,
                "response": {
                    "error": f"unknown output name(s): {sorted(unknown)}; known: {output_names}"
                },
            }
        )
    return None


# --------------------------------------------------------------------------------------
# the public entry point
# --------------------------------------------------------------------------------------


def build_capability(
    run_dir: Path,
    goal: str,
    target: str,
    run_id: str,
    model: str,
    capability_id: str,
    policy: Policy,
    llm: LLMClient | None = None,
) -> Capability:
    raw = (run_dir / "run.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]

    act_events = _dispatched_act_events(events)
    act_events = _prune_dead_ends(act_events)
    act_events = _merge_consecutive_navigations(act_events)

    success_checkpoint: Checkpoint | None = None
    for event in events:
        if event.get("type") == "goal_verified":
            success_checkpoint = Checkpoint(**event["checkpoint_eval"])
            break
    if success_checkpoint is None:
        raise ValueError("run.jsonl has no goal_verified event; cannot record a capability")
    # Stage 4 (Phase 9, B6): generalize BEFORE postconditions are derived -- the last step's
    # postcondition IS this checkpoint (_derive_postcondition's branch (c)).
    success_checkpoint = _generalize_success_checkpoint(success_checkpoint, act_events)

    steps, outputs = _build_steps(act_events)
    steps = _attach_postconditions(steps, act_events, success_checkpoint)
    steps, inputs, value_to_param = _parameterize(steps, goal)
    steps = _canonicalize_steps(steps, value_to_param)
    success_checkpoint = _canonicalize_checkpoint(success_checkpoint, value_to_param)
    # Stage 9 (Phase 9, B5): a goal literal can also leak into a descriptor's own name or a
    # frame_path segment -- neither of which route/value canonicalization above reaches.
    steps = _parameterize_names_and_frames(steps, value_to_param)

    known_outcomes = _seed_known_outcomes(steps, _recorded_urls(steps, success_checkpoint))
    recovery_rules = _seed_recovery_rules(steps, target)

    output_names = [output.name for output in outputs]
    metadata = _propose_metadata(llm, goal, steps, output_names) if llm is not None else None
    if metadata is not None:
        name, description, output_descriptions = metadata
        outputs = [
            output.model_copy(
                update={"description": output_descriptions.get(output.name, output.description)}
            )
            for output in outputs
        ]
    else:
        # D5's graceful degradation: the same name/description `discover` has always recorded.
        name, description = goal, goal

    transcript_path = run_dir / "transcript.jsonl"
    transcript_raw = (
        transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else raw
    )
    transcript_hash = hashlib.sha256(transcript_raw.encode("utf-8")).hexdigest()

    return Capability(
        capability_id=capability_id,
        name=name,
        description=description,
        target=TargetApp(app_id=policy.app_id, entry_point=target),
        inputs=list(inputs.values()),
        outputs=outputs,
        steps=steps,
        success=success_checkpoint,
        known_outcomes=known_outcomes,
        recovery_rules=recovery_rules,
        provenance=Provenance(
            run_id=run_id,
            model=model,
            timestamp=datetime.now(UTC).isoformat(),
            perception_version=PERCEPTION_VERSION,
            transcript_hash=transcript_hash,
        ),
        stability=None,
    )
