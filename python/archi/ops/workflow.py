"""The ops loop as an OKG deployment workflow (ADR 0002 section 7.1, PACT r3-ops-runtime).

This is the only module that imports `okg.deployment`. The worker loads it
through the deployment's `runtime.workflows: [archi.ops.workflow]` (the
renderer writes that line before `okg install` whenever an automation pack is
selected; editing it afterwards makes publish refuse until `okg catalog load
--apply`).

    archi_ops_scan   a short @deployment_scheduled scanner: reads new signals
                     and starts one run per signal with DBOS.start_workflow.
                     Never on the okg-<deployment> queue (an admission submit
                     from a run queued there deadlocks behind it). The workflow
                     id is make_run_id(automation, signal id, attempt).
    archi_ops_run    one run: `archi.ops.loop.run_once` with real dependencies.
                     Every external effect is its own @deployment_step.
                     Anything that touches the clock, the graph, the budget,
                     the records or the mediator is a step, so a recovered run
                     replays its cached answers and repeats nothing. The
                     escalation wait (request_escalation) runs in the workflow
                     body, as DBOS requires.

Proven on a real okg 0.2.0 worker (spike-results.md, 2026-10-25):
- `bind_sdk_context` with our own autocommit connection makes `cost_budget` and
  `request_escalation` work in a plain workflow.
- A late approval is accepted after the worker restarts, so the mediator step
  re-checks halt and the escalation itself (`may_execute`) before it acts.
- Cancel stops a run only at a step boundary and does not wake a recv; halt
  closes each open escalation first (archi.cli.ops).
- Record steps carry the idempotency key "<workflow id>|record-<n>".
"""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from dbos import DBOS, SetWorkflowID

from okg.deployment import (
    cost_budget,
    deployment_scheduled,
    deployment_step,
    deployment_workflow,
    request_escalation,
)

from archi.ops.context_gate import GateCheck, GateFacts, GateResult, GateSpec, evaluate_gate
from archi.ops.escalation import DECISION_SCHEMA, Decision, Escalation, may_execute, subject_for
from archi.ops.loop import (
    BudgetCheck,
    Deps,
    MediationRefused,
    MediatorResult,
    OutcomeResult,
    ReadResult,
    Signal,
    make_run_id,
    run_once,
)
from archi.ops.okg_io import OpsIO
from archi.ops.proposers import Proposed, ProposeRequest, StubProposer
from archi.ops.record import OpsRecords, submit
from archi.ops.runtime_config import RUN_WORKFLOW, SCANNER, RuntimeConfig, load_runtime
from archi.ops.stubs import marker_outcome, resolve_mediator

log = logging.getLogger(__name__)
DECISION_SCHEMA_REF = "schema:literal:" + urllib.parse.quote(json.dumps(DECISION_SCHEMA))


# --- the two private okg imports, in one place ---------------------------------------


def _private_okg():
    """okg 0.2.0 has no public way to bind the SDK context outside the SDK's own
    publish cycle, nor to name its escalation timeout error.

    UPSTREAM ASK (pact/changes/r3-ops-runtime, spike-results.md finding 3): a public
    workflow-context binding. Until then archi pins okg 0.2.0 and imports these two
    names from `okg.deployment._internal`, here only.
    """
    from okg.deployment._internal.context import bind_sdk_context
    from okg.deployment._internal.escalation import EscalationTimeoutError

    return bind_sdk_context, EscalationTimeoutError


def _bind(conn, deployment: str):
    return _private_okg()[0](deployment=deployment, conn=conn)


# --- helpers shared by the steps -------------------------------------------------------


def _runtime() -> RuntimeConfig:
    """Re-read on every call: the instance's ops.yaml and archi.yaml are the source of truth."""
    return load_runtime()


def _io(rt: RuntimeConfig) -> OpsIO:
    return OpsIO(rt.dsn, rt.deployment)


def expected_escalation_id(workflow_id: str) -> str:
    """okg names the escalation of a workflow body "esc-" + sha1("<workflow id>|_create_escalation")[:24]
    (okg 0.2.0, substrate/runtime/escalation.py). If okg changes that, the row is not found
    and the run fails closed (the decision is an error, nothing executes)."""
    return "esc-" + hashlib.sha1(f"{workflow_id}|_create_escalation".encode()).hexdigest()[:24]


def halt_reason(io: OpsIO) -> Optional[str]:
    """Why this run must not act, or None. Halted means the scanner schedule is paused
    (`archi ops halt`) or this run's workflow was cancelled. Raises if it cannot tell."""
    schedules = io.schedules()
    if not schedules:
        raise RuntimeError(f"schedule {SCANNER!r} not found; cannot tell whether the instance is halted")
    if any(schedules.values()):
        return f"the {SCANNER} schedule is paused (archi ops halt)"
    status = DBOS.retrieve_workflow(DBOS.workflow_id).get_status().status
    if str(status).upper() == "CANCELLED":
        return "this run was cancelled"
    return None


def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --- steps ---------------------------------------------------------------------------


@deployment_step(name="archi_ops_now")
def _now_step() -> str:
    return datetime.now(timezone.utc).isoformat()


@deployment_step(name="archi_ops_halted")
def _halted_step() -> Optional[str]:
    return halt_reason(_io(_runtime()))


@deployment_step(name="archi_ops_gate")
def _gate_step(automation: str) -> dict:
    """Source ages from okg's watermarks (`okg doctor --check sources`) against the gate."""
    from archi.bundles.duration import parse_duration
    from archi.bundles.lock import read_lock
    from archi.stack.freshness import parse_timestamp

    rt = _runtime()
    spec: GateSpec = rt.automation(automation).gate
    rows = {r.get("name"): r for r in _io(rt).doctor_sources()}
    try:
        slos = read_lock(rt.instance_dir).get("composed", {}).get("freshness") or {}
    except Exception:  # no lock: sources are held to the gate's own max_staleness, or fail
        slos = {}
    now = datetime.now(timezone.utc)
    freshness = []
    for source in spec.sources:
        row = rows.get(source)
        if row is None:
            continue  # evaluate_gate fails a source that has no row
        stamp = parse_timestamp(((row.get("coverage") or {}).get("watermark") or {}).get("last_reconcile_at"))
        slo = (slos.get(source) or {}).get("max_staleness")
        freshness.append({
            "source": source,
            "age_seconds": max((now - stamp).total_seconds(), 0.0) if stamp else None,
            "max_staleness_seconds": parse_duration(slo) if slo else None,
            "reason": "" if stamp else "no completed run recorded in okg's watermarks",
        })
    result = evaluate_gate(spec, GateFacts(None, tuple(freshness), {}, None))
    return result.as_dict()


@deployment_step(name="archi_ops_pin")
def _pin_step() -> str:
    return _io(_runtime()).pin_generation()


@deployment_step(name="archi_ops_read")
def _read_step(entity: str, generation: str) -> dict:
    """Precedent incidents on the same entity, read at the pinned generation only."""
    rows = _io(_runtime()).read_nodes("ops_incident", generation, {"entity": entity})
    return {
        "rows": rows,
        "context": {"precedents": [{"node_id": r["node_id"], "title": (r.get("attrs") or {}).get("title"),
                                    "status": (r.get("attrs") or {}).get("status")} for r in rows]},
        "node_ids": sorted(r["node_id"] for r in rows),
    }


@deployment_step(name="archi_ops_propose")
def _propose_step(automation: str, request: dict) -> dict:
    cfg = _runtime().settings[automation].proposer
    if cfg.get("kind") != "stub":
        raise RuntimeError(f"automation {automation!r}: proposer kind {cfg.get('kind')!r} is not available yet")
    proposed = StubProposer(cfg.get("fixture") or {}).propose(ProposeRequest(**request))
    return {"proposal": dict(proposed.proposal), "cost": proposed.cost}


@deployment_step(name="archi_ops_budget_check")
def _budget_check_step(scope: str, estimate: float, cap: float) -> dict:
    rt = _runtime()
    with _io(rt).connect() as conn, _bind(conn, rt.deployment):
        with cost_budget(usd=cap, scope=scope) as budget:
            consumed = budget.consumed
    return {"ok": consumed + estimate <= cap + 1e-9, "detail": f"{consumed:g} of {cap:g} spent"}


@deployment_step(name="archi_ops_budget_record")
def _budget_record_step(scope: str, cost: float, consumption_id: str) -> None:
    rt = _runtime()
    with _io(rt).connect() as conn, _bind(conn, rt.deployment):
        with cost_budget(usd=_cap_of(conn, scope), scope=scope) as budget:
            # The money is already spent: record it even if it passes the cap, so the ledger
            # tells the truth and the next check sees the overrun.
            budget.consume(cost, consumption_id=consumption_id, enforce_cap=False)


def _cap_of(conn, scope: str) -> float:
    row = conn.execute("SELECT cap_usd FROM okg.deployment_cost_caps WHERE scope = %s", (scope,)).fetchone()
    if row is None:
        raise RuntimeError(f"cost_budget scope {scope!r} has no cap; check() runs before record()")
    return float(row[0])


@deployment_step(name="archi_ops_record")
def _record_step(payload: dict, key: str) -> str:
    rt = _runtime()
    records = OpsRecords(nodes={n["node_id"]: n for n in payload["nodes"]}, edges=list(payload["edges"]))
    reply = submit(records, deployment=rt.deployment, dsn=rt.dsn, idempotency_key=key)
    return str((reply.get("output") or reply).get("status"))


@deployment_step(name="archi_ops_mediate")
def _mediate_step(automation: str, action_class: str, payload: dict, proposal_id: str,
                  idempotency_key: str, esc: Optional[dict]) -> dict:
    """The only place an action happens. Its first statements re-check halt and the escalation."""
    rt = _runtime()
    io = _io(rt)
    reason = halt_reason(io)  # 1. halted?
    if reason:
        return {"refused": ["halted", reason]}
    if esc is None:  # 2. the escalation row, read again from okg.escalation_live
        return {"refused": ["error", "no escalation was opened for this proposal"]}
    escalation = Escalation(esc["escalation_id"], esc["proposal_id"], _moment(esc["opened_at"]),
                            _moment(esc["timeout_at"]))
    decision = io.decision_for(escalation.escalation_id)
    allowed = may_execute(decision, escalation, datetime.now(timezone.utc))
    if not allowed:
        return {"refused": ["timed_out" if allowed.late else "error", allowed.reason]}
    mediator = rt.automation(automation).action_classes[action_class].mediator  # 3. act
    out = resolve_mediator(mediator)(payload=payload, context={
        "dsn": rt.dsn, "proposal_id": proposal_id, "idempotency_key": idempotency_key,
        "workflow_id": DBOS.workflow_id})
    return {"status": out["status"], "result": out.get("result", "")}


@deployment_step(name="archi_ops_outcome")
def _outcome_step(automation: str, entity: str) -> dict:
    rt = _runtime()
    cfg = rt.settings[automation].outcome
    if cfg.get("kind") != "marker":
        return {"result": "unknown", "resolution": "no outcome check is configured"}
    result, resolution = marker_outcome(dsn=rt.dsn, entity=entity)
    return {"result": result, "resolution": resolution}


# --- the real dependencies -------------------------------------------------------------


class _Clock:
    def now(self) -> datetime:
        return _moment(_now_step())


class _Halt:
    def halted(self) -> bool:
        return _halted_step() is not None


class _Gate:
    def __init__(self, automation: str):
        self.automation = automation

    def check(self, spec: GateSpec) -> GateResult:
        d = _gate_step(self.automation)
        return GateResult(d["generation"], tuple(GateCheck(**c) for c in d["checks"]))


class _Reader:
    def __init__(self):
        #: the full rows the pinned read returned, by node id, so the recorder can carry
        #: a cited precedent with the proposal (okg admits an edge only with both ends in the payload)
        self.rows: dict[str, dict] = {}

    def pin(self) -> str:
        return _pin_step()

    def read(self, signal: Signal, generation: str) -> ReadResult:
        d = _read_step(signal.entity, generation)
        self.rows = {r["node_id"]: r for r in d["rows"]}
        return ReadResult(d["context"], frozenset(d["node_ids"]))


class _Proposer:
    def __init__(self, automation: str):
        self.automation = automation

    def propose(self, request: ProposeRequest) -> Proposed:
        d = _propose_step(self.automation, {**asdict(request), "context": dict(request.context)})
        return Proposed(d["proposal"], d["cost"])


class _Budget:
    def check(self, scope: str, estimate: float, cap: float) -> BudgetCheck:
        d = _budget_check_step(scope, estimate, cap)
        return BudgetCheck(d["ok"], d["detail"])

    def record(self, scope: str, cost: float) -> None:
        _budget_record_step(scope, cost, f"{DBOS.workflow_id}|{scope}")


class _Recorder:
    """Collects records in memory; `flush` submits them all in one admission step.

    Each flush carries every record so far (a record linked to an earlier one must
    travel with it), with the key "<workflow id>|record-<n>"."""

    def __init__(self, reader: _Reader):
        self.records = OpsRecords()
        self.reader = reader
        self.flushes = 0
        self.last = ""

    def add(self, kind: str, *, evidence=(), **attrs: Any) -> str:
        evidence = list(evidence)
        for cited in evidence:  # a cited precedent travels with the proposal that cites it
            if str(cited).startswith("ops_incident:") and cited in self.reader.rows:
                self.records.carry(self.reader.rows[cited])
        return self.records.add(kind, evidence=evidence, **attrs)

    def flush(self) -> None:
        payload = self.records.payload()
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if digest == self.last or not payload["nodes"]:
            return
        self.flushes += 1
        status = _record_step(payload, f"{DBOS.workflow_id}|record-{self.flushes}")
        if status != "accepted":
            raise RuntimeError(f"okg admission did not accept the ops records: {status}")
        self.last = digest


class _Decider:
    """Opens the escalation through request_escalation and reads the decision back from okg."""

    def __init__(self, rt: RuntimeConfig):
        self.rt, self.reason = rt, ""

    def open(self, *, automation, proposal_id, proposal, timeout_at) -> Escalation:
        self.reason = (f"{automation}: {proposal.summary}\nAction: {proposal.action_class} "
                       f"{json.dumps(dict(proposal.payload), sort_keys=True)}\nRollback: {proposal.rollback}\n"
                       f"Evidence: {', '.join(proposal.evidence_ids)}")
        return Escalation(expected_escalation_id(DBOS.workflow_id), proposal_id, _Clock().now(), timeout_at)

    def wait(self, escalation: Escalation) -> Decision:
        timeout_error = _private_okg()[1]
        remaining = max((escalation.timeout_at - _Clock().now()).total_seconds(), 1.0)
        try:
            request_escalation(subject=subject_for(escalation.proposal_id), reason=self.reason,
                               decision_schema=DECISION_SCHEMA_REF, timeout_seconds=remaining)
        except timeout_error:
            pass  # okg marked the row timed_out; the row below says so
        return _io(self.rt).decision_for(escalation.escalation_id)


class _Mediator:
    def __init__(self, automation: str):
        self.automation = automation

    def execute(self, *, action_class, payload, proposal_id, idempotency_key, generation, escalation=None):
        esc = None if escalation is None else {
            "escalation_id": escalation.escalation_id, "proposal_id": escalation.proposal_id,
            "opened_at": escalation.opened_at.isoformat(), "timeout_at": escalation.timeout_at.isoformat()}
        d = _mediate_step(self.automation, action_class, dict(payload), proposal_id, idempotency_key, esc)
        if "refused" in d:
            raise MediationRefused(*d["refused"])
        return MediatorResult(d["status"], d["result"])


class _Outcome:
    def __init__(self, automation: str):
        self.automation = automation

    def check(self, *, signal: Signal, action: MediatorResult, generation: str) -> OutcomeResult:
        d = _outcome_step(self.automation, signal.entity)
        return OutcomeResult(d["result"], d["resolution"])


def real_deps(rt: RuntimeConfig, automation: str) -> Deps:
    reader = _Reader()
    return Deps(gate=_Gate(automation), reader=reader, proposer=_Proposer(automation),
                decider=_Decider(rt), mediator=_Mediator(automation), outcome=_Outcome(automation),
                recorder=_Recorder(reader), halt=_Halt(), clock=_Clock(), budget=_Budget())


# --- the workflows ---------------------------------------------------------------------


@deployment_workflow(name=RUN_WORKFLOW)
def archi_ops_run(automation: str, signal: dict) -> dict:
    """One run of one automation for one signal. The workflow id is the run id."""
    rt = _runtime()
    config = rt.automation(automation)
    sig = Signal(id=signal["id"], entity=signal["entity"], title=signal["title"],
                 source=signal.get("source") or "signal", summary=signal.get("summary"))
    with _io(rt).connect() as conn, _bind(conn, rt.deployment):
        result = run_once(config, sig, real_deps(rt, automation), run_id=DBOS.workflow_id)
    return {**asdict(result), "records": list(result.records)}


@deployment_scheduled(SCANNER, default_cron="*/1 * * * *", automatic_backfill=False, guard_body=False)
def archi_ops_scan(scheduled_time, context):
    """Start one run per new signal. Short: the runs are separate workflows (queue None)."""
    rt = _runtime()
    io = _io(rt)
    if any(io.schedules().values()):
        log.warning("archi_ops_scan: halted (schedule paused); starting nothing")
        return
    for name in sorted(rt.settings):
        for s in io.new_signals(name):
            run_id = make_run_id(name, s["id"], s["attempt"])
            with SetWorkflowID(run_id):
                DBOS.start_workflow(archi_ops_run, name, {k: s[k] for k in ("id", "entity", "title", "source", "summary")})
            io.mark_signal_started(name, s["id"])
            log.info("archi_ops_scan: started %s", run_id)
