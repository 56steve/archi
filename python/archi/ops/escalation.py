"""Escalation decisions for L2 proposals (ADR 0002 section 7.3, D13).

An L2 automation proposes; it acts only after a person approves through an
okg escalation. This module holds the parts that need no database:

- the schema of the decision payload a person answers with
  (`{"verdict": "approve" | "reject", "note": "..."}`), and
- the deadline rule: a proposal executes only if it was approved, at a known
  decision time, no later than the proposal's timeout.

The okg calls (open an escalation, read its state, respond) live in the
workflow module, not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

VERDICTS = ("approve", "reject")

#: The schema the escalation is opened with (okg `schema:literal:` decision schema).
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "note": {"type": "string"},
    },
    "required": ["verdict"],
    "additionalProperties": False,
}

#: Where a decision can end up. Only `approved` can lead to execution.
APPROVED, REJECTED, TIMED_OUT, ERROR = "approved", "rejected", "timed_out", "error"
STATUSES = (APPROVED, REJECTED, TIMED_OUT, ERROR)


class DecisionPayloadError(ValueError):
    """A decision payload that does not match DECISION_SCHEMA."""


def parse_decision_payload(payload: Any) -> tuple[str, Optional[str]]:
    """Return `(verdict, note)` or raise DecisionPayloadError."""
    if not isinstance(payload, Mapping):
        raise DecisionPayloadError("decision payload must be an object")
    extra = sorted(set(payload) - {"verdict", "note"})
    if extra:
        raise DecisionPayloadError(f"unknown decision keys {extra}")
    verdict = payload.get("verdict")
    if verdict not in VERDICTS:
        raise DecisionPayloadError(f"verdict must be one of {list(VERDICTS)}, got {verdict!r}")
    note = payload.get("note")
    if note is not None and not isinstance(note, str):
        raise DecisionPayloadError("note must be a string")
    return verdict, note


def status_from_payload(payload: Any) -> str:
    """`approved` or `rejected` for a valid payload."""
    verdict, _ = parse_decision_payload(payload)
    return APPROVED if verdict == "approve" else REJECTED


@dataclass(frozen=True)
class Decision:
    """What the Decider reports about one escalation."""

    status: str
    escalation_id: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    #: The proposal's deadline. The loop sets this from its own clock; it does
    #: not trust the Decider's copy.
    timeout_at: Optional[datetime] = None
    note: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class Execution:
    """Whether a decision lets the mediator run, and if not, why."""

    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def may_execute(decision: Decision, now: datetime) -> Execution:
    """True only for an approval made at or before the deadline.

    An approval that arrives late, has no decision time, or has no deadline to
    check against does not execute: a decision we cannot show was in time was
    not in time. `now` guards against a decision time in the future.
    """
    if decision.status != APPROVED:
        return Execution(False, f"decision is {decision.status}, not approved")
    if decision.timeout_at is None:
        return Execution(False, "no deadline to check the approval against")
    if decision.decided_at is None:
        return Execution(False, "approval has no decision time")
    if decision.decided_at > now:
        return Execution(False, f"decision time {decision.decided_at.isoformat()} is after now")
    if decision.decided_at > decision.timeout_at:
        return Execution(
            False,
            f"approved at {decision.decided_at.isoformat()}, after the deadline "
            f"{decision.timeout_at.isoformat()}",
        )
    return Execution(True, "approved within the deadline")
