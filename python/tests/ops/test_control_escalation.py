"""Pure parts of req.r3-ops-runtime.control and the escalation deadline rule."""
from datetime import datetime, timedelta, timezone

import pytest

from archi.ops.control import BudgetUsage, RunInfo, build_status, halt_plan, resume_plan, shadow_summary
from archi.ops.escalation import (
    DECISION_SCHEMA,
    Decision,
    DecisionPayloadError,
    may_execute,
    parse_decision_payload,
    status_from_payload,
)
from archi.ops.loop import DEMOTED_PREFIX, AutomationConfig

T0 = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


def at(s):
    return T0 + timedelta(seconds=s)


def test_decision_payload_schema():
    assert DECISION_SCHEMA["properties"]["verdict"]["enum"] == ["approve", "reject"]
    assert parse_decision_payload({"verdict": "approve", "note": "ok"}) == ("approve", "ok")
    assert status_from_payload({"verdict": "reject"}) == "rejected"
    for bad in ({}, {"verdict": "maybe"}, {"verdict": "approve", "x": 1}, {"verdict": "approve", "note": 3}, "approve"):
        with pytest.raises(DecisionPayloadError):
            parse_decision_payload(bad)


@pytest.mark.parametrize("decision, ok", [
    (Decision("approved", decided_at=at(100), timeout_at=at(600)), True),
    (Decision("approved", decided_at=at(600), timeout_at=at(600)), True),
    (Decision("approved", decided_at=at(601), timeout_at=at(600)), False),
    (Decision("approved", decided_at=None, timeout_at=at(600)), False),
    (Decision("approved", decided_at=at(100), timeout_at=None), False),
    (Decision("approved", decided_at=at(900), timeout_at=at(2000)), False),  # after now
    (Decision("rejected", decided_at=at(100), timeout_at=at(600)), False),
    (Decision("timed_out", timeout_at=at(600)), False),
    (Decision("error", error="x", timeout_at=at(600)), False),
    (Decision("surprise", decided_at=at(1), timeout_at=at(600)), False),
])
def test_may_execute(decision, ok):
    assert bool(may_execute(decision, at(700))) is ok
    assert may_execute(decision, at(700)).reason


def automation(name, level="L2", mode="live"):
    return AutomationConfig(name, level, "L2", "L2", mode)


def test_status_rows():
    rows = build_status(
        [automation("a", "L1"), automation("b", "L3"), automation("c", mode="shadow")],
        paused_schedules={"c"},
        budgets={"a": BudgetUsage(1.0, 10.0, 10.0)},
        proposals=[
            {"automation": "a", "mode": "shadow", "text": DEMOTED_PREFIX + "stale"},
            {"automation": "a", "mode": "shadow"},
            {"automation": "a", "mode": "live"},
        ],
    )
    a, b, c = rows
    assert (a.level, a.budget.exhausted, a.budget.day_remaining) == ("L1", True, 0.0)
    assert (a.shadow.shadow_proposals, a.shadow.live_proposals, a.shadow.demotions) == (2, 1, 1)
    assert b.level is None and "D13" in b.level_problem
    assert c.schedule_paused and c.mode == "shadow" and c.budget.day_remaining is None
    assert a.as_dict()["budget"]["exhausted"] is True
    assert shadow_summary([]) == {}


def test_halt_and_resume_plans():
    schedules = {"scan": False, "a": False, "b": True}
    runs = [RunInfo("w1", "a", "PENDING"), RunInfo("w2", "a", "ENQUEUED"), RunInfo("w3", "b", "SUCCESS"),
            RunInfo("w4", "b", "CANCELLED"), RunInfo("w5", "b", "DELAYED")]
    plan = halt_plan(schedules, runs)
    assert plan.pause == ("a", "scan") and plan.already_paused == ("b",)
    assert [r.workflow_id for r in plan.cancel] == ["w1", "w2", "w5"]
    assert [r.workflow_id for r in plan.final] == ["w3", "w4"]
    # Resume un-pauses what halt paused, and leaves "b", which an operator paused earlier.
    after_halt = {"scan": True, "a": True, "b": True}
    assert resume_plan(after_halt, paused_by_halt=plan.pause) == ("a", "scan")
    assert resume_plan({"scan": False, "a": True}, paused_by_halt=plan.pause) == ("a",)
    assert halt_plan({}, []) == halt_plan({}, [])
