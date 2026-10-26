"""req.r3-ops-self-monitoring.health: the signals the instance-health automation reads, as plain data."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from archi.health import signals
from archi.health.config import HealthConfig
from archi.health.problems import Problem, ProblemError, make_key, slug

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def doctor(*checks):
    return {"ok": not any(c["status"] == "fail" for c in checks), "instance": "/i", "checks": list(checks)}


def check(id_, status, message="m", remediation="", footgun="", data=None):
    return {"id": id_, "status": status, "message": message, "remediation": remediation, "footgun": footgun,
            "data": data or {}}


def freshness(*rows):
    return check("source-freshness", "fail", "2 of 3 sources over their max_staleness", "okg ingest",
                 data={"sources": list(rows)})


def row(source, ok, last="2026-09-20T10:00:00+00:00", limit="24h", age="35d", reason="last run 35d ago, over the 24h limit"):
    return {"source": source, "ok": ok, "last_reconcile_at": last, "max_staleness": limit, "age": age, "reason": reason}


# --- keys ------------------------------------------------------------------------


def test_a_key_names_the_check_and_the_subject_and_a_file():
    assert make_key("source-freshness", "jira.jira") == "source-freshness/jira.jira"
    assert make_key("services", "weird name!") == "services/weird_name"
    assert slug("source-freshness/jira.jira") == "source-freshness--jira.jira"
    with pytest.raises(ProblemError):
        slug("no-subject")
    with pytest.raises(ProblemError):
        Problem(key="a/b", family="nonsense", title="t", detail="d")


# --- doctor ------------------------------------------------------------------------


def test_each_late_source_is_its_own_problem_and_the_fresh_ones_are_not():
    report = doctor(freshness(row("jira.jira", False), row("docsite.docs", True, age="1h"),
                              row("twiki.twiki", False, last=None, reason="no completed run")))
    problems = signals.doctor_problems(report)
    assert [p.key for p in problems] == ["source-freshness/jira.jira", "source-freshness/twiki.twiki"]
    assert all(p.family == "doctor" and p.severity == "error" for p in problems)
    assert "2026-09-20T10:00:00+00:00" in problems[0].detail and "24h" in problems[0].detail
    assert "none recorded" in problems[1].detail


def test_a_late_source_issue_text_has_no_age_so_it_does_not_change_between_runs():
    a = signals.doctor_problems(doctor(freshness(row("jira.jira", False, age="35d"))))
    b = signals.doctor_problems(doctor(freshness(row("jira.jira", False, age="36d", reason="last run 36d ago"))))
    assert a == b


def test_other_failing_checks_are_one_problem_each_with_the_remediation():
    report = doctor(check("services", "fail", "mcp: absent", "archi up", "FG-16"), check("published", "pass"),
                    check("okg-lint", "skip"), check("data-root", "warn", "slow"))
    problems = signals.doctor_problems(report)
    assert [p.key for p in problems] == ["services/instance"]
    assert "archi up" in problems[0].detail and "FG-16" in problems[0].detail


def test_warnings_count_only_when_asked_and_ignored_checks_never():
    report = doctor(check("data-root", "warn", "slow"), check("services", "fail", "down"))
    assert [p.key for p in signals.doctor_problems(report, include_warnings=True)] == ["data-root/instance", "services/instance"]
    assert signals.doctor_problems(report, include_warnings=True)[0].severity == "warning"
    assert [p.key for p in signals.doctor_problems(report, ignore=["services"])] == []


# --- ingest ------------------------------------------------------------------------


def test_a_blocked_frontier_is_an_ingest_problem_and_a_fresh_one_is_not():
    report = {"rows": [
        {"registry_source_name": "jira.jira", "frontier_state": "blocked", "last_attempt_status": "FAILED",
         "last_attempt_run_id": "run-9", "last_error": "401 from jira", "read_visibility_state": "last_run_failed"},
        {"registry_source_name": "docsite.docs", "frontier_state": "fresh"},
        {"registry_source_name": "twiki.twiki", "frontier_state": "publish_pending"},
    ]}
    (problem,) = signals.frontier_problems(report)
    assert problem.key == "ingest/jira.jira" and problem.family == "ingest"
    assert "401 from jira" in problem.detail and "run-9" in problem.detail


# --- automation errors ---------------------------------------------------------------


def decisions(automation, verdicts, hours_ago=1):
    when = (NOW - timedelta(hours=hours_ago)).isoformat()
    return [{"node_id": f"ops_decision:{automation}-{i}", "attrs": {"automation": automation, "verdict": v, "decided_at": when}}
            for i, v in enumerate(verdicts)]


def reader(rows):
    return lambda subtype: rows if subtype == "ops_decision" else []


def test_an_automation_whose_decisions_are_mostly_errors_is_a_problem():
    rows = decisions("triage", ["error", "error", "observed", "invalid_proposal"]) + decisions("health", ["observed"] * 5)
    (problem,) = signals.automation_error_problems(reader(rows), NOW)
    assert problem.key == "automation-errors/triage"
    assert "3 of the last 4" in problem.detail


def test_too_few_decisions_or_old_ones_do_not_make_a_rate():
    assert signals.automation_error_problems(reader(decisions("t", ["error", "error"])), NOW) == []
    old = decisions("t", ["error"] * 5, hours_ago=48)
    assert signals.automation_error_problems(reader(old), NOW) == []
    capped = decisions("t", ["budget_exhausted"] * 5 + ["halted"] * 5)
    assert signals.automation_error_problems(reader(capped), NOW) == []


# --- eval -------------------------------------------------------------------------


def report_file(tmp_path, name, finished, score):
    d = tmp_path / "eval" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"finished_at": finished, "score": score}))


def test_with_no_eval_report_the_signal_is_skipped_and_says_so(tmp_path):
    problems, why = signals.eval_problems(tmp_path)
    assert problems == [] and "no eval report exists" in why


def test_a_score_well_below_the_recent_mean_is_a_regression(tmp_path):
    for i, score in enumerate([0.80, 0.82, 0.81, 0.70]):
        report_file(tmp_path, f"r{i}.json", f"2026-10-0{i + 1}T00:00:00Z", score)
    (problem,), why = signals.eval_problems(tmp_path)
    assert why is None and problem.key == "eval/score" and "0.700" in problem.detail


def test_a_steady_score_is_not_a_problem_and_one_report_is_no_trend(tmp_path):
    report_file(tmp_path, "a.json", "2026-10-01T00:00:00Z", 0.8)
    assert signals.eval_problems(tmp_path)[1] is not None
    report_file(tmp_path, "b.json", "2026-10-02T00:00:00Z", 0.79)
    assert signals.eval_problems(tmp_path) == ([], None)


# --- collect --------------------------------------------------------------------------


def collect(tmp_path, *, doctor_report=None, frontier=None, rows=(), **kw):
    def fail(msg):
        def go():
            raise RuntimeError(msg)
        return go

    return signals.collect(
        HealthConfig(), instance_dir=tmp_path, now=NOW,
        read_doctor=kw.get("read_doctor") or (lambda: doctor_report or doctor()),
        read_frontier=kw.get("read_frontier") or (lambda: frontier or {"rows": []}),
        read_ops_records=reader(list(rows)),
    )


def test_collect_gathers_every_signal_and_reports_the_eval_skip(tmp_path):
    obs = collect(tmp_path, doctor_report=doctor(freshness(row("jira.jira", False))))
    assert [p.key for p in obs.problems] == ["source-freshness/jira.jira"]
    assert obs.scope == ["doctor", "ingest", "automation-errors", "monitor"]  # eval was not judged
    assert [s["signal"] for s in obs.skipped] == ["eval"] and "no eval report" in obs.skipped[0]["reason"]


def test_a_signal_that_cannot_be_read_becomes_a_monitor_problem_and_leaves_its_scope(tmp_path):
    def boom():
        raise RuntimeError("okg is down")

    obs = collect(tmp_path, read_frontier=boom)
    assert [p.key for p in obs.problems] == ["monitor/ingest"]
    assert "okg is down" in obs.problems[0].detail
    assert "ingest" not in obs.scope and "doctor" in obs.scope and "monitor" in obs.scope


def test_a_malformed_eval_report_is_a_monitor_problem_not_a_skip(tmp_path):
    (tmp_path / "eval" / "reports").mkdir(parents=True)
    (tmp_path / "eval" / "reports" / "bad.json").write_text("{not json")
    obs = collect(tmp_path)
    assert [p.key for p in obs.problems] == ["monitor/eval"] and obs.skipped == []
