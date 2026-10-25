"""The operations loop (ADR 0002 section 7.1, PACT r3-ops-runtime req.loop).

One run takes a signal through: halt check, effective level, context gate,
pinned generation, proposal, decision by level, mediator, outcome check, and
the ops records of each step. This module is plain Python with no okg imports;
everything it touches outside itself comes in through the small Protocols in
`Deps`, so tests can fake each one. The workflow module wraps each Protocol
call in a durable step.

Levels (ADR 0002 7.3, D13). The effective level is the minimum of the
instance's setting, the bundle's bound and the pack's `max_level`. L3 and any
level the runtime does not know are refused at run start.

    L0  record the incident, the proposal and an `observed` decision.
    L1  as L0, with the draft on the proposal and a `drafted` decision.
        Nothing is sent.
    L2  the Decider opens an escalation. Only an approval made at or before
        the deadline reaches the mediator; HaltCheck runs once more as the
        statement before the mediator call.

Shadow mode (a live automation whose context gate fails drops to shadow for
the run; a shadow automation stays there) never opens an escalation and never
mediates. It records what it would have done, with an `observed` decision.

Records use the verdicts `approved`, `rejected` and `timed_out` that the
ops-record schema documents, plus `observed`, `drafted`, `budget_exhausted`,
`invalid_proposal`, `halted` and `error`. A decision needs a proposal to link
to, so a run that stops after the incident is recorded (budget, a refused
proposal, a proposer error) records a placeholder proposal that cites the
incident and says why there is no real one.

`AutomationConfig` is what the loop needs from an automation. It will be fed
from the rendered automation block (instance level, bundle bound, pack
max_level, gate, action classes, budget, timeout).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

from archi.ops.context_gate import GateResult, GateSpec
from archi.ops.escalation import APPROVED, ERROR, REJECTED, TIMED_OUT, Decision, may_execute
from archi.ops.proposers import ProposeRequest, Proposer

ADR = "ADR 0002 D13"
#: The highest level 3.0 runs.
MAX_LEVEL = 2
MODES = ("shadow", "live")
#: Proposal text prefix when a live automation was dropped to shadow.
DEMOTED_PREFIX = "demoted to shadow: "
ACTOR_KIND = "automation"

PROPOSAL_KEYS = frozenset({"summary", "evidence_ids", "action_class", "payload", "confidence", "rollback", "draft"})


# --- configuration -----------------------------------------------------------------


class LevelRefused(ValueError):
    """A level the 3.0 runtime will not run."""


def parse_level(value: Any, *, what: str = "level") -> int:
    """`L0`/`L1`/`L2` (or 0, 1, 2) as an int. L3 and anything unknown are refused."""
    n: Optional[int] = None
    if isinstance(value, bool):
        n = None
    elif isinstance(value, int):
        n = value
    elif isinstance(value, str) and len(value) == 2 and value[0] in "Ll" and value[1].isdigit():
        n = int(value[1])
    if n is None or n < 0:
        raise LevelRefused(f"{what} {value!r} is not a known autonomy level (L0, L1, L2); {ADR}")
    if n > MAX_LEVEL:
        raise LevelRefused(f"{what} L{n} is above L2; 3.0 stops at L2 ({ADR})")
    return n


@dataclass(frozen=True)
class ActionClassConfig:
    max_level: Any
    mediator: str = ""
    #: payload keys the class requires
    payload_required: tuple[str, ...] = ()


@dataclass(frozen=True)
class AutomationConfig:
    name: str
    #: the instance's setting, the bundle's upper bound and the pack's max_level
    instance_level: Any
    bundle_level: Any
    pack_max_level: Any
    #: every automation starts in shadow
    mode: str = "shadow"
    gate: Optional[GateSpec] = None
    action_classes: Mapping[str, ActionClassConfig] = field(default_factory=dict)
    #: extra proposal keys the pack's proposal schema requires
    proposal_required: tuple[str, ...] = ()
    #: how long an L2 escalation waits for a person
    timeout_seconds: int = 4 * 3600


def effective_level(automation: AutomationConfig) -> int:
    """The minimum of the three levels; LevelRefused if any is L3 or unknown,
    or if any action class allows more than L2."""
    levels = [
        parse_level(automation.instance_level, what="instance level"),
        parse_level(automation.bundle_level, what="bundle bound"),
        parse_level(automation.pack_max_level, what="pack max_level"),
    ]
    for name, cls in automation.action_classes.items():
        parse_level(cls.max_level, what=f"action class {name!r} max_level")
    return min(levels)


# --- the signal and the proposal ---------------------------------------------------


@dataclass(frozen=True)
class Signal:
    id: str
    entity: str
    title: str
    #: what raised it: "schedule", "ticket", "alert"
    source: str = "signal"
    summary: Optional[str] = None
    entity_kind: Optional[str] = None
    error_signature: Optional[str] = None
    severity: Optional[str] = None
    ticket_keys: tuple[str, ...] = ()
    detected_at: Optional[datetime] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "entity": self.entity, "title": self.title, "source": self.source,
            "summary": self.summary, "entity_kind": self.entity_kind,
            "error_signature": self.error_signature, "severity": self.severity,
            "ticket_keys": list(self.ticket_keys),
        }


class ProposalError(ValueError):
    """The proposal does not match the automation's proposal schema."""


@dataclass(frozen=True)
class Proposal:
    summary: str
    evidence_ids: tuple[str, ...]
    action_class: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    confidence: Optional[float] = None
    rollback: Optional[str] = None
    draft: Optional[str] = None


def validate_proposal(raw: Any, automation: AutomationConfig, level: int) -> Proposal:
    """Check a raw proposal against the automation's proposal schema.

    Cited evidence ids are always required. An action class is required at L2
    and, when the automation declares classes, must be one of them.
    """
    if not isinstance(raw, Mapping):
        raise ProposalError("proposal must be an object")
    errors: list[str] = []
    unknown = sorted(set(raw) - PROPOSAL_KEYS)
    if unknown:
        errors.append(f"unknown keys {unknown}")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary is required")
    evidence = raw.get("evidence_ids")
    if (not isinstance(evidence, (list, tuple)) or not evidence
            or not all(isinstance(e, str) and e.strip() for e in evidence)):
        errors.append("evidence_ids must list at least one cited okg node id")
    for key in automation.proposal_required:
        if raw.get(key) in (None, "", [], {}):
            errors.append(f"{key} is required by the proposal schema")

    action_class = raw.get("action_class")
    payload = raw.get("payload", {})
    if action_class is not None and not isinstance(action_class, str):
        errors.append("action_class must be a string")
    elif action_class is None and level >= 2:
        errors.append("action_class is required at L2")
    elif action_class is not None and automation.action_classes:
        cls = automation.action_classes.get(action_class)
        if cls is None:
            errors.append(f"action_class {action_class!r} is not declared; known: {sorted(automation.action_classes)}")
        elif isinstance(payload, Mapping):
            absent = [k for k in cls.payload_required if k not in payload]
            if absent:
                errors.append(f"payload for {action_class!r} is missing {absent}")
    if not isinstance(payload, Mapping):
        errors.append("payload must be an object")
    confidence = raw.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1
    ):
        errors.append("confidence must be a number from 0 to 1")
    rollback = raw.get("rollback")
    if rollback is not None and not isinstance(rollback, str):
        errors.append("rollback must be a string")
    draft = raw.get("draft")
    if draft is not None and not isinstance(draft, str):
        errors.append("draft must be a string")
    if level == 1 and not (isinstance(draft, str) and draft.strip()):
        errors.append("draft is required at L1")
    if errors:
        raise ProposalError("; ".join(errors))
    return Proposal(
        summary=summary,
        evidence_ids=tuple(dict.fromkeys(evidence)),
        action_class=action_class,
        payload=dict(payload),
        confidence=None if confidence is None else float(confidence),
        rollback=rollback,
        draft=draft,
    )


# --- dependencies ------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCheck:
    ok: bool
    #: "run" or "day" when not ok
    scope: Optional[str] = None
    detail: str = ""


class BudgetGuard(Protocol):
    def check(self, automation: str) -> BudgetCheck:
        """Is there budget left for one more proposal, per run and per day?"""


class GateChecker(Protocol):
    def check(self, spec: GateSpec) -> GateResult: ...


class Reader(Protocol):
    def pin(self) -> str:
        """The generation this run reads. Called once."""

    def read(self, signal: Signal, generation: str) -> Mapping[str, Any]:
        """The context for the signal, read at `generation` only."""


class Decider(Protocol):
    def decide(self, *, automation: str, proposal_id: str, proposal: Proposal, timeout_at: datetime) -> Decision:
        """Open an escalation and wait. Return approved, rejected, timed_out or error."""


@dataclass(frozen=True)
class MediatorResult:
    status: str  # succeeded | failed
    result: str = ""


class Mediator(Protocol):
    def execute(
        self, *, action_class: str, payload: Mapping[str, Any], proposal_id: str, idempotency_key: str,
        generation: str,
    ) -> MediatorResult: ...


@dataclass(frozen=True)
class OutcomeResult:
    result: str  # resolved | not_resolved | partial | unknown
    resolution: str = ""


class OutcomeChecker(Protocol):
    def check(self, *, signal: Signal, action: MediatorResult, generation: str) -> OutcomeResult: ...


class Recorder(Protocol):
    def add(self, kind: str, *, evidence: Iterable[Any] = (), **attrs: Any) -> str:
        """Add one ops record in `archi.ops.record.OpsRecords.add` format; return its node id."""


class HaltCheck(Protocol):
    def halted(self) -> bool: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True)
class Deps:
    gate: GateChecker
    reader: Reader
    proposer: Proposer
    decider: Decider
    mediator: Mediator
    outcome: OutcomeChecker
    recorder: Recorder
    halt: HaltCheck
    clock: Clock
    budget: BudgetGuard


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


# --- the run -----------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    run_id: str
    #: executed | observed | drafted | rejected | timed_out | halted | refused |
    #: budget_exhausted | invalid_proposal | error
    status: str
    reason: str = ""
    level: Optional[int] = None
    mode: Optional[str] = None
    generation: Optional[str] = None
    #: node ids of the ops records written, in order
    records: tuple[str, ...] = ()
    mediated: bool = False
    outcome: Optional[str] = None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_once(automation: AutomationConfig, signal: Signal, deps: Deps) -> RunResult:
    """Run the loop once for one signal. See the module docstring."""
    clock = deps.clock
    started = clock.now()
    run_id = f"{automation.name}-{signal.id}-{int(started.timestamp())}"

    # 1. halt. Nothing has been read yet, so there is no generation to record against.
    if deps.halt.halted():
        return RunResult(run_id, "halted", "halted before the run started")

    # 2. effective level and mode
    try:
        level = effective_level(automation)
    except LevelRefused as exc:
        return RunResult(run_id, "refused", str(exc))
    if automation.mode not in MODES:
        return RunResult(run_id, "refused", f"mode {automation.mode!r} is not one of {list(MODES)}")
    mode = automation.mode

    # 3. context gate: a failing gate drops this run to shadow (req.shadow demote hook)
    gate = deps.gate.check(automation.gate) if automation.gate is not None else None
    # 4. pin the generation, once
    generation = deps.reader.pin()
    failure: Optional[str] = None
    if gate is not None and not gate.ok:
        failure = gate.message
    elif gate is not None and gate.generation not in (None, generation):
        failure = f"context gate read generation {gate.generation} but the run pinned {generation}"
    mode_reason: Optional[str] = None
    if failure is not None:
        mode_reason = (DEMOTED_PREFIX if mode == "live" else "context gate failed: ") + failure
        mode = "shadow"

    nodes: list[str] = []
    common = {"automation": automation.name, "actor_kind": ACTOR_KIND, "actor": automation.name,
              "generation_read": generation}

    def write(kind: str, record_id: str, *, evidence: Iterable[Any] = (), **attrs: Any) -> str:
        nid = deps.recorder.add(kind, evidence=evidence, record_id=record_id, **{**common, **attrs})
        nodes.append(nid)
        return nid

    def result(status: str, reason: str = "", **extra: Any) -> RunResult:
        return RunResult(run_id, status, reason, level=level, mode=mode, generation=generation,
                         records=tuple(nodes), **extra)

    incident_id = f"inc-{run_id}"
    proposal_id = f"prop-{run_id}"
    decision_id = f"dec-{run_id}"
    incident_node = write(
        "incident", incident_id, title=signal.title, summary=signal.summary, entity=signal.entity,
        entity_kind=signal.entity_kind, error_signature=signal.error_signature, severity=signal.severity,
        status="open", detected_at=_iso(signal.detected_at or started),
        ticket_keys=list(signal.ticket_keys) or None, signal=f"{signal.source}:{signal.id}",
    )

    def stop(verdict: str, reason: str) -> RunResult:
        """Record a placeholder proposal and a decision, then end the run."""
        write("proposal", proposal_id, evidence=[incident_node], incident_id=incident_id,
              summary=f"no proposal: {reason}", autonomy_level=f"L{level}", mode=mode,
              proposed_at=_iso(clock.now()), text=mode_reason)
        write("decision", decision_id, proposal_id=proposal_id, verdict=verdict, reason=reason,
              decided_at=_iso(clock.now()))
        return result(verdict, reason)

    # 5. budget, before the model is asked
    budget = deps.budget.check(automation.name)
    if not budget.ok:
        return stop("budget_exhausted", f"{budget.scope or 'budget'} budget exhausted: {budget.detail}".rstrip(": "))

    # 6. propose, from the pinned generation only
    try:
        context = deps.reader.read(signal, generation)
        raw = deps.proposer.propose(ProposeRequest(automation.name, signal.as_dict(), context, generation, level))
    except Exception as exc:  # a failed run is recorded, not hidden
        return stop(ERROR, f"proposer failed: {type(exc).__name__}: {exc}")
    try:
        proposal = validate_proposal(raw, automation, level)
    except ProposalError as exc:
        return stop("invalid_proposal", f"proposal refused: {exc}")

    payload = dict(proposal.payload)
    if proposal.draft is not None:
        payload = {"draft": proposal.draft, **({"payload": payload} if payload else {})}
    write(
        "proposal", proposal_id, evidence=proposal.evidence_ids, incident_id=incident_id,
        summary=proposal.summary, action_class=proposal.action_class,
        payload=json.dumps(payload, sort_keys=True) if payload else None,
        confidence=proposal.confidence, rollback=proposal.rollback, autonomy_level=f"L{level}",
        mode=mode, proposed_at=_iso(clock.now()), text=mode_reason,
    )

    # 7. decide by level
    if mode == "shadow":
        would = {0: "observe", 1: "draft for a person to send", 2: "escalate for approval and then act"}[level]
        write("decision", decision_id, proposal_id=proposal_id, verdict="observed",
              reason=f"shadow: recorded only; a live run at L{level} would {would}", decided_at=_iso(clock.now()))
        return result("observed", "shadow")
    if level == 0:
        write("decision", decision_id, proposal_id=proposal_id, verdict="observed",
              reason="L0: observed only", decided_at=_iso(clock.now()))
        return result("observed", "L0")
    if level == 1:
        write("decision", decision_id, proposal_id=proposal_id, verdict="drafted",
              reason="L1: draft recorded for a person to send; nothing was sent", decided_at=_iso(clock.now()))
        return result("drafted", "L1")

    # L2: escalate. The deadline is ours; the Decider's copy is not trusted.
    timeout_at = clock.now() + timedelta(seconds=automation.timeout_seconds)
    try:
        decision = deps.decider.decide(automation=automation.name, proposal_id=proposal_id,
                                       proposal=proposal, timeout_at=timeout_at)
    except Exception as exc:
        decision = Decision(ERROR, error=f"{type(exc).__name__}: {exc}")
    decision = replace(decision, timeout_at=timeout_at)
    verdict_at = clock.now()
    allowed = may_execute(decision, verdict_at)

    person = decision.status in (APPROVED, REJECTED) and decision.decided_by is not None
    recorded_status = decision.status
    if decision.status == APPROVED and not allowed:
        recorded_status = TIMED_OUT
    reason = {
        APPROVED: "approved by escalation",
        REJECTED: "rejected by escalation",
        TIMED_OUT: "no decision before the deadline",
        ERROR: f"escalation failed: {decision.error or 'unknown error'}",
    }.get(decision.status)
    if reason is None:
        recorded_status, reason = ERROR, f"unknown decision status {decision.status!r}"
    if decision.status == APPROVED and not allowed:
        reason = f"not executed: {allowed.reason}"
    if decision.note:
        reason = f"{reason}; note: {decision.note}"
    write(
        "decision", decision_id, proposal_id=proposal_id, verdict=recorded_status,
        escalation_id=decision.escalation_id, reason=reason,
        decided_at=_iso(decision.decided_at or verdict_at),
        **({"actor_kind": "person", "actor": decision.decided_by} if person else {}),
    )
    if not allowed:
        return result(recorded_status, reason)

    # 8. halt, as the statement before the mediator call
    if deps.halt.halted():
        write("decision", f"{decision_id}-halted", proposal_id=proposal_id, verdict="halted",
              reason="halted after approval, before the mediator ran", decided_at=_iso(clock.now()))
        return result("halted", "halted before the mediator ran")
    action = _mediate(deps, automation, proposal, proposal_id, generation, run_id)
    action_id = f"act-{run_id}"
    write(
        "action", action_id, proposal_id=proposal_id, decision_id=decision_id,
        action_class=proposal.action_class,
        mediator=(automation.action_classes.get(proposal.action_class) or ActionClassConfig(0)).mediator or None,
        payload=json.dumps(dict(proposal.payload), sort_keys=True) if proposal.payload else None,
        status=action.status, result=action.result, executed_at=_iso(clock.now()),
    )

    # 9. outcome check
    try:
        outcome = deps.outcome.check(signal=signal, action=action, generation=generation)
    except Exception as exc:
        outcome = OutcomeResult("unknown", f"outcome check failed: {type(exc).__name__}: {exc}")
    write(
        "outcome", f"out-{run_id}", incident_id=incident_id, action_id=action_id, result=outcome.result,
        resolution=outcome.resolution or None, check_method="outcome_check", checked_at=_iso(clock.now()),
    )
    return result("executed", f"mediator {action.status}", mediated=True, outcome=outcome.result)


def _mediate(deps: Deps, automation: AutomationConfig, proposal: Proposal, proposal_id: str,
             generation: str, run_id: str) -> MediatorResult:
    # Nothing may sit between the HaltCheck in run_once and this call.
    try:
        return deps.mediator.execute(
            action_class=proposal.action_class or "", payload=dict(proposal.payload),
            proposal_id=proposal_id, idempotency_key=run_id, generation=generation,
        )
    except Exception as exc:
        return MediatorResult("failed", f"{type(exc).__name__}: {exc}")
