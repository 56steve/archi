"""Fakes for every Protocol in archi.ops.loop, shared by the loop tests."""
from datetime import datetime, timedelta, timezone

from archi.ops.context_gate import GateCheck, GateResult
from archi.ops.escalation import Escalation
from archi.ops.loop import BudgetCheck, MediatorResult, OutcomeResult, ReadResult
from archi.ops.record import OpsRecords

T0 = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
GEN = "gen-7"
NODES = frozenset({"jira:A-1", "jira:A-2"})
PASS = GateResult(GEN, (GateCheck("freshness:s", True, "fresh"),))
FAIL = GateResult(GEN, (GateCheck("freshness:s", False, "last run 3d ago, over the 1d limit"),))


def at(seconds=0):
    return T0 + timedelta(seconds=seconds)


def iso(seconds=0):
    return at(seconds).strftime("%Y-%m-%dT%H:%M:%SZ")


class Clock:
    def __init__(self, start=T0):
        self.t = start

    def now(self):
        return self.t


class Gate:
    def __init__(self, result, raises=None):
        self.result, self.raises, self.calls = result, raises, 0

    def check(self, spec):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.result


class Reader:
    def __init__(self, pin_raises=None, nodes=NODES):
        self.pins, self.reads, self.pin_raises, self.nodes = 0, [], pin_raises, nodes

    def pin(self):
        self.pins += 1
        if self.pin_raises:
            raise self.pin_raises
        return GEN

    def read(self, signal, generation):
        self.reads.append((signal.id, generation))
        return ReadResult({"rows": 3}, self.nodes)


class Decider:
    """Opens an escalation (esc-1) and answers `after` seconds later, moving the clock there."""

    def __init__(self, clock, decision, after=60, open_proposal_id=None, open_raises=None):
        self.clock, self.decision, self.after = clock, decision, after
        self.open_proposal_id, self.open_raises = open_proposal_id, open_raises
        self.opened, self.waited = [], []

    def open(self, **kw):
        self.opened.append(kw)
        if self.open_raises:
            raise self.open_raises
        return Escalation("esc-1", self.open_proposal_id or kw["proposal_id"], self.clock.now(), kw["timeout_at"])

    def wait(self, escalation):
        self.waited.append(escalation)
        self.clock.t = at(self.after)
        return self.decision

    @property
    def calls(self):
        return self.opened + self.waited


class Mediator:
    def __init__(self, result=None, refuse=None):
        self.calls, self.result, self.refuse = [], result or MediatorResult("succeeded", "restarted"), refuse

    def execute(self, **kw):
        self.calls.append(kw)
        if self.refuse:
            raise self.refuse
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
        self.records, self.calls, self.flushed_at = OpsRecords(), [], []

    def flush(self):
        self.flushes = getattr(self, "flushes", 0) + 1
        self.flushed_at.append(len(self.calls))

    def add(self, kind, *, evidence=(), **attrs):
        evidence = list(evidence)
        self.calls.append((kind, evidence, {k: v for k, v in attrs.items() if v is not None}))
        return self.records.add(kind, evidence=evidence, **attrs)


class Halt:
    """Answers from `answers` in turn (the last repeats); an Exception in it is raised."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def halted(self):
        self.calls += 1
        answer = self.answers[min(self.calls, len(self.answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Budget:
    def __init__(self, spent=None, raises=None):
        self.spent, self.raises, self.checks, self.recorded = dict(spent or {}), raises, [], []

    def check(self, scope, estimate, cap):
        self.checks.append((scope, estimate, cap))
        if self.raises:
            raise self.raises
        spent = self.spent.get(scope, 0.0)
        return BudgetCheck(spent + estimate <= cap, f"{spent:g} of {cap:g} spent")

    def record(self, scope, cost):
        self.recorded.append((scope, cost))
        self.spent[scope] = self.spent.get(scope, 0.0) + cost
