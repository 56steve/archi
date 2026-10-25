"""req.r3-ops-runtime.loop: the operations loop at L0, L1 and L2 with fakes for
every dependency (acc.r3-ops-runtime.timeout, acc.r3-ops-runtime.no-l3, the
run-start half). Each test asserts the exact ops records written and whether
the mediator was called."""
from datetime import datetime, timedelta, timezone

import pytest

from archi.ops.context_gate import GateCheck, GateResult, GateSpec
from archi.ops.escalation import Decision
from archi.ops.loop import (
    DEMOTED_PREFIX,
    ActionClassConfig,
    AutomationConfig,
    BudgetCheck,
    Deps,
    MediatorResult,
    OutcomeResult,
    Signal,
    run_once,
)
from archi.ops.proposers import StubProposer
from archi.ops.record import OpsRecords

T0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
TIMEOUT = 600
RUN = f"triage-sig-1-{int(T0.timestamp())}"
GEN = "gen-7"
SIGNAL = Signal(id="sig-1", entity="T2_US_MIT", title="Site down", source="ticket")
PROPOSAL = {
    "summary": "restart the agent",
    "evidence_ids": ["jira:A-1"],
    "action_class": "restart_service",
    "payload": {"service": "xrootd"},
    "confidence": 0.9,
    "rollback": "start it again",
}


def iso(seconds=0):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- fakes ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = T0

    def now(self):
        return self.t


class Gate:
    def __init__(self, result):
        self.result, self.calls = result, 0

    def check(self, spec):
        self.calls += 1
        return self.result


class Reader:
    def __init__(self):
        self.pins, self.reads = 0, []

    def pin(self):
        self.pins += 1
        return GEN

    def read(self, signal, generation):
        self.reads.append((signal.id, generation))
        return {"rows": 3}


class Decider:
    """Answers after `after` seconds, moving the shared clock to that moment."""

    def __init__(self, clock, decision, after):
        self.clock, self.decision, self.after, self.calls = clock, decision, after, []

    def decide(self, **kw):
        self.calls.append(kw)
        self.clock.t = T0 + timedelta(seconds=self.after)
        return self.decision


class Mediator:
    def __init__(self, result=None):
        self.calls, self.result = [], result or MediatorResult("succeeded", "restarted")

    def execute(self, **kw):
        self.calls.append(kw)
        return self.result


class Outcome:
    def __init__(self):
        self.calls = []

    def check(self, **kw):
        self.calls.append(kw)
        return OutcomeResult("resolved", "service up")


class Recorder:
    """Records through the real OpsRecords, so the schema vets every record."""

    def __init__(self):
        self.records, self.calls = OpsRecords(), []

    def add(self, kind, *, evidence=(), **attrs):
        evidence = list(evidence)
        self.calls.append((kind, evidence, {k: v for k, v in attrs.items() if v is not None}))
        return self.records.add(kind, evidence=evidence, **attrs)


class Halt:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def halted(self):
        self.calls += 1
        return self.answers[min(self.calls, len(self.answers)) - 1]


class Budget:
    def __init__(self, check=BudgetCheck(True)):
        self.result, self.calls = check, []

    def check(self, automation):
        self.calls.append(automation)
        return self.result


PASS = GateResult(GEN, (GateCheck("freshness:s", True, "fresh"),))
FAIL = GateResult(GEN, (GateCheck("freshness:s", False, "last run 3d ago, over the 1d limit"),))


def config(level="L2", mode="live", **over):
    base = dict(
        name="triage", instance_level=level, bundle_level="L2", pack_max_level="L2", mode=mode,
        gate=GateSpec(sources=("s",)),
        action_classes={"restart_service": ActionClassConfig("L2", "slurm.restart", ("service",))},
        timeout_seconds=TIMEOUT,
    )
    base.update(over)
    return AutomationConfig(**base)


class World:
    def __init__(self, proposal=PROPOSAL, gate=PASS, decision=None, after=60, halt=(False,), budget=None):
        self.clock = Clock()
        self.gate, self.reader = Gate(gate), Reader()
        self.proposer = StubProposer({"default": proposal})
        self.decider = Decider(self.clock, decision, after)
        self.mediator, self.outcome = Mediator(), Outcome()
        self.recorder, self.halt = Recorder(), Halt(*halt)
        self.budget = budget or Budget()

    def run(self, automation):
        deps = Deps(self.gate, self.reader, self.proposer, self.decider, self.mediator, self.outcome,
                    self.recorder, self.halt, self.clock, self.budget)
        return run_once(automation, SIGNAL, deps)

    @property
    def kinds(self):
        return [c[0] for c in self.recorder.calls]


# --- expected records ----------------------------------------------------------------

COMMON = {"automation": "triage", "actor_kind": "automation", "actor": "triage", "generation_read": GEN}


def rec(kind, *, evidence=(), **attrs):
    return (kind, list(evidence), {**COMMON, **attrs})


def incident():
    return rec("incident", record_id=f"inc-{RUN}", title="Site down", entity="T2_US_MIT", status="open",
               detected_at=iso(), signal="ticket:sig-1")


def proposal(level="L2", mode="live", at=0, **over):
    attrs = dict(record_id=f"prop-{RUN}", incident_id=f"inc-{RUN}", summary="restart the agent",
                 action_class="restart_service", payload='{"service": "xrootd"}', confidence=0.9,
                 rollback="start it again", autonomy_level=level, mode=mode, proposed_at=iso(at))
    attrs.update(over)
    return rec("proposal", evidence=["jira:A-1"], **attrs)


def decision(verdict, reason, at=0, **over):
    attrs = dict(record_id=f"dec-{RUN}", proposal_id=f"prop-{RUN}", verdict=verdict, reason=reason,
                 decided_at=iso(at))
    attrs.update(over)
    return rec("decision", **attrs)


APPROVED = Decision("approved", escalation_id="esc-1", decided_by="alice", decided_at=T0 + timedelta(seconds=60))
ALICE = {"actor_kind": "person", "actor": "alice", "escalation_id": "esc-1"}


def action(at):
    return rec("action", record_id=f"act-{RUN}", proposal_id=f"prop-{RUN}", decision_id=f"dec-{RUN}",
               action_class="restart_service", mediator="slurm.restart", payload='{"service": "xrootd"}',
               status="succeeded", result="restarted", executed_at=iso(at))


def outcome(at):
    return rec("outcome", record_id=f"out-{RUN}", incident_id=f"inc-{RUN}", action_id=f"act-{RUN}",
               result="resolved", resolution="service up", check_method="outcome_check", checked_at=iso(at))


# --- L0, L1 --------------------------------------------------------------------------


def test_l0_records_observed_and_never_acts():
    w = World()
    result = w.run(config("L0"))
    assert result.status == "observed" and not result.mediated
    assert w.recorder.calls == [
        incident(),
        proposal("L0"),
        decision("observed", "L0: observed only"),
    ]
    assert w.decider.calls == [] and w.mediator.calls == []
    assert w.reader.pins == 1 and w.reader.reads == [("sig-1", GEN)]
    assert w.recorder.records.dangling() == []


def test_l1_records_the_draft_and_never_sends():
    draft = {"summary": "reply to the site", "evidence_ids": ["jira:A-1"], "draft": "Hello, we restarted it."}
    w = World(proposal=draft)
    result = w.run(config("L1"))
    assert result.status == "drafted" and not result.mediated
    assert w.recorder.calls == [
        incident(),
        rec("proposal", evidence=["jira:A-1"], record_id=f"prop-{RUN}", incident_id=f"inc-{RUN}",
            summary="reply to the site", payload='{"draft": "Hello, we restarted it."}',
            autonomy_level="L1", mode="live", proposed_at=iso()),
        decision("drafted", "L1: draft recorded for a person to send; nothing was sent"),
    ]
    assert w.decider.calls == [] and w.mediator.calls == []


def test_l1_proposal_without_a_draft_is_refused():
    w = World(proposal=PROPOSAL)
    result = w.run(config("L1"))
    assert result.status == "invalid_proposal"
    assert "draft is required at L1" in result.reason
    assert w.mediator.calls == []


# --- L2 ------------------------------------------------------------------------------


def test_l2_approved_runs_the_mediator_then_checks_the_outcome():
    w = World(decision=APPROVED)
    result = w.run(config())
    assert (result.status, result.mediated, result.outcome) == ("executed", True, "resolved")
    assert w.recorder.calls == [
        incident(),
        proposal(),
        decision("approved", "approved by escalation", at=60, **ALICE),
        action(60),
        outcome(60),
    ]
    assert w.mediator.calls == [{
        "action_class": "restart_service", "payload": {"service": "xrootd"}, "proposal_id": f"prop-{RUN}",
        "idempotency_key": RUN, "generation": GEN,
    }]
    assert w.decider.calls[0]["timeout_at"] == T0 + timedelta(seconds=TIMEOUT)
    assert w.halt.calls == 2  # at the start, and as the statement before the mediator
    assert len(result.records) == 5 and w.recorder.records.dangling() == []


def test_l2_rejected_never_reaches_the_mediator():
    w = World(decision=Decision("rejected", escalation_id="esc-1", decided_by="alice",
                                decided_at=T0 + timedelta(seconds=60), note="wrong site"))
    result = w.run(config())
    assert result.status == "rejected" and not result.mediated
    assert w.recorder.calls == [
        incident(), proposal(),
        decision("rejected", "rejected by escalation; note: wrong site", at=60, **ALICE),
    ]
    assert w.mediator.calls == [] and w.outcome.calls == []


def test_l2_timeout_provably_does_not_execute():
    w = World(decision=Decision("timed_out", escalation_id="esc-1"), after=TIMEOUT)
    result = w.run(config())
    assert result.status == "timed_out" and not result.mediated
    assert w.recorder.calls == [
        incident(), proposal(),
        decision("timed_out", "no decision before the deadline", at=TIMEOUT, escalation_id="esc-1"),
    ]
    assert w.mediator.calls == [] and w.outcome.calls == []
    assert "action" not in w.kinds


def test_l2_late_approval_does_not_execute():
    late = Decision("approved", escalation_id="esc-1", decided_by="alice",
                    decided_at=T0 + timedelta(seconds=TIMEOUT + 1))
    w = World(decision=late, after=TIMEOUT + 100)
    result = w.run(config())
    assert result.status == "timed_out" and not result.mediated
    expected_reason = (f"not executed: approved at {iso(TIMEOUT + 1).replace('Z', '+00:00')}, "
                       f"after the deadline {iso(TIMEOUT).replace('Z', '+00:00')}")
    assert w.recorder.calls == [
        incident(), proposal(),
        decision("timed_out", expected_reason, at=TIMEOUT + 1, **ALICE),
    ]
    assert w.mediator.calls == [] and w.halt.calls == 1


def test_loop_uses_its_own_deadline_not_the_deciders():
    # The decider claims a far-off deadline; the approval is still late by ours.
    late = Decision("approved", decided_by="alice", decided_at=T0 + timedelta(seconds=TIMEOUT + 1),
                    timeout_at=T0 + timedelta(days=9))
    w = World(decision=late, after=TIMEOUT + 2)
    assert w.run(config()).status == "timed_out"
    assert w.mediator.calls == []


def test_halt_before_the_mediator_stops_an_approved_run():
    w = World(decision=APPROVED, halt=(False, True))
    result = w.run(config())
    assert result.status == "halted" and not result.mediated
    assert w.recorder.calls == [
        incident(), proposal(),
        decision("approved", "approved by escalation", at=60, **ALICE),
        decision("halted", "halted after approval, before the mediator ran", at=60,
                 record_id=f"dec-{RUN}-halted"),
    ]
    assert w.mediator.calls == [] and "action" not in w.kinds


def test_halt_at_the_start_does_nothing():
    w = World(decision=APPROVED, halt=(True,))
    result = w.run(config())
    assert result.status == "halted" and result.records == ()
    assert w.recorder.calls == [] and w.gate.calls == 0 and w.reader.pins == 0


def test_escalation_error_does_not_execute():
    w = World(decision=Decision("error", error="boom"))
    result = w.run(config())
    assert result.status == "error" and w.mediator.calls == []
    assert w.recorder.calls[-1] == decision("error", "escalation failed: boom", at=60)


def test_mediator_failure_is_recorded_and_the_outcome_still_checked():
    w = World(decision=APPROVED)
    w.mediator = Mediator(MediatorResult("failed", "exit 1"))
    result = w.run(config())
    assert result.status == "executed"
    kind, _, attrs = w.recorder.calls[3]
    assert (kind, attrs["status"], attrs["result"]) == ("action", "failed", "exit 1")


# --- L3, unknown levels --------------------------------------------------------------


@pytest.mark.parametrize("over", [
    {"instance_level": "L3"},
    {"bundle_level": "L3"},
    {"pack_max_level": "L3"},
    {"instance_level": "L9"},
    {"instance_level": "act"},
    {"instance_level": None},
    {"action_classes": {"restart_service": ActionClassConfig("L3")}},
])
def test_level_above_l2_or_unknown_is_refused_at_run_start(over):
    w = World(decision=APPROVED)
    result = w.run(config(**over))
    assert result.status == "refused"
    assert "ADR 0002 D13" in result.reason or "3.0 stops at L2" in result.reason
    assert w.recorder.calls == [] and w.gate.calls == 0 and w.reader.pins == 0
    assert w.proposer.calls == [] and w.mediator.calls == []


def test_effective_level_is_the_minimum():
    w = World(proposal={"summary": "s", "evidence_ids": ["jira:A-1"], "draft": "d"})
    result = w.run(config("L2", bundle_level="L1"))
    assert (result.level, result.status) == (1, "drafted")


# --- gate, shadow, budget, proposal checks -------------------------------------------


def test_failing_gate_drops_a_live_run_to_shadow_with_the_reason_recorded():
    w = World(decision=APPROVED, gate=FAIL)
    result = w.run(config())
    reason = DEMOTED_PREFIX + FAIL.message
    assert result.status == "observed" and result.mode == "shadow" and not result.mediated
    assert w.recorder.calls == [
        incident(),
        proposal(mode="shadow", text=reason),
        decision("observed", "shadow: recorded only; a live run at L2 would escalate for approval and then act"),
    ]
    assert "freshness:s" in reason
    assert w.decider.calls == [] and w.mediator.calls == []


def test_shadow_never_mediates_even_with_a_passing_gate():
    w = World(decision=APPROVED)
    result = w.run(config(mode="shadow"))
    assert result.status == "observed" and result.mode == "shadow"
    assert w.recorder.calls == [
        incident(),
        proposal(mode="shadow"),
        decision("observed", "shadow: recorded only; a live run at L2 would escalate for approval and then act"),
    ]
    assert w.decider.calls == [] and w.mediator.calls == [] and w.outcome.calls == []


def test_shadow_with_a_failing_gate_keeps_the_reason_without_claiming_a_demotion():
    w = World(gate=FAIL)
    w.run(config(mode="shadow", instance_level="L0"))
    assert w.recorder.calls[1][2]["text"] == "context gate failed: " + FAIL.message


def test_gate_and_pin_on_different_generations_demote_to_shadow():
    w = World(gate=GateResult("gen-6", PASS.checks), decision=APPROVED)
    result = w.run(config())
    assert result.mode == "shadow" and w.mediator.calls == []
    assert "gen-6" in w.recorder.calls[1][2]["text"] and GEN in w.recorder.calls[1][2]["text"]


def test_budget_exhausted_records_a_decision_and_stops_before_proposing():
    w = World(decision=APPROVED, budget=Budget(BudgetCheck(False, "day", "spent 5 of 5")))
    result = w.run(config())
    reason = "day budget exhausted: spent 5 of 5"
    assert result.status == "budget_exhausted"
    assert w.recorder.calls == [
        incident(),
        rec("proposal", evidence=[f"ops_incident:inc-{RUN}"], record_id=f"prop-{RUN}", incident_id=f"inc-{RUN}",
            summary=f"no proposal: {reason}", autonomy_level="L2", mode="live", proposed_at=iso()),
        decision("budget_exhausted", reason),
    ]
    assert w.budget.calls == ["triage"]
    assert w.proposer.calls == [] and w.decider.calls == [] and w.mediator.calls == []


def test_proposal_without_evidence_ids_is_refused():
    bare = {k: v for k, v in PROPOSAL.items() if k != "evidence_ids"}
    w = World(proposal=bare, decision=APPROVED)
    result = w.run(config())
    reason = "proposal refused: evidence_ids must list at least one cited okg node id"
    assert result.status == "invalid_proposal" and result.reason == reason
    assert w.recorder.calls[-1] == decision("invalid_proposal", reason)
    assert w.decider.calls == [] and w.mediator.calls == []


@pytest.mark.parametrize("bad, fragment", [
    ({"evidence_ids": []}, "evidence_ids"),
    ({"evidence_ids": [""]}, "evidence_ids"),
    ({"action_class": "drop_database"}, "not declared"),
    ({"action_class": None}, "required at L2"),
    ({"payload": {}}, "missing ['service']"),
    ({"confidence": 3}, "confidence"),
    ({"surprise": 1}, "unknown keys"),
])
def test_proposal_schema_violations_are_refused(bad, fragment):
    w = World(proposal={**PROPOSAL, **bad}, decision=APPROVED)
    result = w.run(config())
    assert result.status == "invalid_proposal" and fragment in result.reason
    assert w.mediator.calls == []


def test_proposer_failure_is_recorded_as_an_error():
    w = World()
    w.proposer = StubProposer({"fail": {"sig-1": "model timeout"}})
    result = w.run(config())
    assert result.status == "error"
    assert w.recorder.calls[-1] == decision("error", "proposer failed: ProposerError: model timeout")
    assert w.mediator.calls == []
