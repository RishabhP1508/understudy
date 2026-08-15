"""TargetDescriptor and resolution: role + accessible name, never a ref, never CSS.

Phase 2 stub: exact match on (role, name), with an ordinal to disambiguate when the name is
empty or shared by more than one element in that observation. Resolution requires a unique
match; an ambiguous match is a hard error, never first-match-wins. Phase 4 replaces this with
the full ranked strategy list (role+name, then scope, then relational hints, then ordinal, with
a CSS fallback last and marked brittle).
"""

from __future__ import annotations

from pydantic import BaseModel

from understudy.models.observation import Observation


class TargetDescriptor(BaseModel):
    role: str
    name: str
    ordinal: int | None = None


class AmbiguousTarget(Exception):
    """More than one element matches (role, name) and no ordinal was given to disambiguate."""


class TargetNotFound(Exception):
    """No element matches (role, name), or the given ordinal is out of range."""


def resolve(observation: Observation, descriptor: TargetDescriptor) -> str:
    """Return the node_id of the unique element matching descriptor, or raise."""
    matches = [
        element
        for element in observation.elements
        if element.role == descriptor.role and element.name == descriptor.name
    ]
    if not matches:
        raise TargetNotFound(
            f"no element matches role={descriptor.role!r} name={descriptor.name!r}"
        )
    if descriptor.ordinal is None:
        if len(matches) > 1:
            raise AmbiguousTarget(
                f"{len(matches)} elements match role={descriptor.role!r} "
                f"name={descriptor.name!r}; an ordinal is required to disambiguate"
            )
        return matches[0].node_id
    if not 0 <= descriptor.ordinal < len(matches):
        raise TargetNotFound(
            f"ordinal {descriptor.ordinal} out of range for role={descriptor.role!r} "
            f"name={descriptor.name!r} ({len(matches)} matches)"
        )
    return matches[descriptor.ordinal].node_id
