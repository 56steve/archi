"""Workflow steps an action class may name as its mediator (ADR 0002 section 7.2).

This registry is the one place steps are registered; the runtime adds its
implementations against these names. A step is a mediator for work with **no
external effect**, so its highest level is L0 or L1. Anything that changes
an external system (a ticket update, a restart) must be a live tool from a
tool pack: an automation never calls an external system any other way.

    record_finding   L0  writes a finding to the ops record
    draft_reply      L1  stores a draft reply for a person to send; never sends
"""
from __future__ import annotations

from dataclasses import dataclass

from archi.ops.levels import level_problem, rank

#: The highest level a workflow step may carry. L2 means an approved
#: escalation runs it against an external system, which only a live tool may do.
STEP_CEILING = "L1"


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    max_level: str
    description: str


_STEPS: dict[str, WorkflowStep] = {}


def register_step(name: str, max_level: str, description: str) -> WorkflowStep:
    """Register a step. Refuses a repeated name, a level above L1 and an unknown level."""
    message = level_problem(max_level, f"step {name!r} max_level")
    if message:
        raise ValueError(message)
    if rank(max_level) > rank(STEP_CEILING):
        raise ValueError(
            f"step {name!r} max_level {max_level}: a workflow step has no external effect, "
            f"so it stops at {STEP_CEILING}; an action that changes an external system "
            "must be a live tool from a tool pack (ADR 0002 section 7.2)"
        )
    if name in _STEPS:
        raise ValueError(f"workflow step {name!r} is already registered")
    step = WorkflowStep(name, max_level, description)
    _STEPS[name] = step
    return step


def get_step(name: str) -> WorkflowStep | None:
    return _STEPS.get(name)


def step_names() -> tuple[str, ...]:
    return tuple(_STEPS)


register_step("record_finding", "L0", "Write a finding to the ops record. Nothing else changes.")
register_step("draft_reply", "L1", "Store a draft reply for a person to send. Never sends it.")
