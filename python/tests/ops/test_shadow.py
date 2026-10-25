"""req.r3-ops-runtime.shadow: a shadow report is produced from a fixture
history, scoring proposals against later human actions
(acc.r3-ops-runtime.score)."""
import json
from pathlib import Path

import pytest

from archi.ops import shadow
from archi.ops.record import RecordError, load_records
from archi.ops.shadow import promotion_readiness, render_markdown, score_automation

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shadow" / "history.jsonl"
UNTIL = "2026-10-31T00:00:00Z"


def _rows():
    return [{"node_id": n["node_id"], "subtype": n["subtype"], "attrs": dict(n["attrs"], text="okg-stamped")}
            for n in load_records(FIXTURE).payload()["nodes"]]


def _reader(rows):
    def read(subtype, attr, value):
        return [r for r in rows if r["subtype"] == subtype and r["attrs"].get(attr) == value]
    return read


def _classes(report):
    return {d["proposal_id"]: d["class"] for d in report["details"]}


def test_report_from_the_fixture_history_has_exact_numbers():
    report = score_automation(_reader(_rows()), "ticket-triage", until=UNTIL)
    assert _classes(report) == {
        "prop-agree": "agree",
        "prop-disagree": "disagree",
        "prop-false": "false",
        "prop-undecidable": "undecidable",
        "prop-late": "late",
        "prop-approved": "agree",
        "prop-pending": "pending",
    }
    assert report["proposals"] == 7 and report["scored"] == 6
    assert report["ignored_non_shadow"] == 1
    assert report["counts"] == {"agree": 2, "disagree": 1, "false": 1, "late": 1,
                                "undecidable": 1, "pending": 1}
    assert report["agreement_rate"] == 0.6667          # 2 / (2 + 1)
    assert report["response_coverage"] == 0.5           # 3 / 6
    assert report["false_proposal_rate"] == 0.2         # 1 / (6 - 1)
    assert report["false_proposal_rate_upper"] == 0.3333  # (1 + 1) / 6

    ttfr = report["time_to_first_response"]
    assert ttfr["automation"] == {"n": 7, "median": 900, "p90": 1800}
    assert ttfr["human"] == {"n": 4, "median": 4950, "p90": 432600}
    assert ttfr["median_lead_seconds"] == 3300
    assert ttfr["excluded"] == 0
    json.dumps(report)


def test_the_late_response_is_not_agreement_but_counts_for_response_time():
    report = score_automation(_reader(_rows()), "ticket-triage", until=UNTIL)
    assert _classes(report)["prop-late"] == "late"
    assert 5 * 86400 + 600 in [report["time_to_first_response"]["human"]["p90"]]


def test_a_longer_horizon_turns_the_late_response_into_agreement():
    report = score_automation(_reader(_rows()), "ticket-triage", until=UNTIL, horizon_hours=7 * 24)
    assert _classes(report)["prop-late"] == "agree"
    assert report["counts"]["late"] == 0 and report["counts"]["agree"] == 3


def test_window_selects_proposals_by_time():
    report = score_automation(_reader(_rows()), "ticket-triage", since="2026-09-16T00:00:00Z",
                              until="2026-09-30T00:00:00Z")
    assert set(_classes(report)) == {"prop-false", "prop-undecidable", "prop-late", "prop-approved"}
    assert report["agreement_rate"] == 1.0  # only prop-approved was answered in horizon


def test_key_fields_limit_what_must_match():
    # prop-disagree differs only in `service`; with only `site` as key it agrees.
    report = score_automation(_reader(_rows()), "ticket-triage", until=UNTIL,
                              key_fields={"restart_storage_door": ["site"]})
    assert _classes(report)["prop-disagree"] == "agree"


def test_a_person_rejection_is_disagreement():
    rows = _rows()
    rows.append({"node_id": "ops_decision:d-rej", "subtype": "ops_decision",
                 "attrs": {"record_id": "d-rej", "proposal_id": "prop-undecidable", "verdict": "rejected",
                           "decided_at": "2026-09-17T07:00:00Z", "actor_kind": "person", "actor": "x"}})
    report = score_automation(_reader(rows), "ticket-triage", until=UNTIL)
    assert _classes(report)["prop-undecidable"] == "disagree"


def test_a_timed_out_policy_decision_is_not_a_human_response():
    rows = _rows()
    rows.append({"node_id": "ops_decision:d-to", "subtype": "ops_decision",
                 "attrs": {"record_id": "d-to", "proposal_id": "prop-undecidable", "verdict": "timed_out",
                           "decided_at": "2026-09-17T07:00:00Z", "actor_kind": "automation", "actor": "policy"}})
    report = score_automation(_reader(rows), "ticket-triage", until=UNTIL)
    assert _classes(report)["prop-undecidable"] == "undecidable"


def test_no_proposals_gives_an_empty_report():
    report = score_automation(_reader(_rows()), "no-such-automation", until=UNTIL)
    assert report["proposals"] == 0 and report["scored"] == 0
    assert report["agreement_rate"] is None and report["false_proposal_rate"] is None
    assert report["false_proposal_rate_upper"] is None
    assert report["time_to_first_response"]["automation"] == {"n": 0, "median": None, "p90": None}
    assert "n/a" in render_markdown(report)


def test_no_human_actions_leaves_agreement_undefined_and_everything_undecidable():
    rows = [r for r in _rows() if not (r["attrs"].get("actor_kind") == "person")]
    report = score_automation(_reader(rows), "ticket-triage", until=UNTIL)
    assert report["counts"]["agree"] == report["counts"]["disagree"] == report["counts"]["late"] == 0
    assert report["agreement_rate"] is None
    assert report["time_to_first_response"]["human"]["n"] == 0
    assert report["counts"]["undecidable"] == 5 and report["counts"]["false"] == 1
    assert not promotion_readiness(report, min_proposals=1)["ready"]


def test_an_incident_without_detected_at_is_excluded_from_response_time():
    rows = _rows()
    for r in rows:
        if r["node_id"] == "ops_incident:inc-false":
            del r["attrs"]["detected_at"]
    report = score_automation(_reader(rows), "ticket-triage", until=UNTIL)
    assert report["time_to_first_response"]["excluded"] == 1
    assert report["time_to_first_response"]["automation"]["n"] == 6


def test_a_resolved_incident_alone_never_makes_a_proposal_false():
    rows = _rows()
    rows.append({"node_id": "ops_outcome:o1", "subtype": "ops_outcome",
                 "attrs": {"record_id": "o1", "incident_id": "inc-undecidable", "result": "resolved",
                           "checked_at": "2026-09-18T00:00:00Z", "actor_kind": "automation", "actor": "a"}})
    report = score_automation(_reader(rows), "ticket-triage", until=UNTIL)
    assert _classes(report)["prop-undecidable"] == "undecidable"


def test_an_explicit_no_action_needed_outcome_makes_a_proposal_false():
    rows = _rows()
    rows.append({"node_id": "ops_outcome:o2", "subtype": "ops_outcome",
                 "attrs": {"record_id": "o2", "incident_id": "inc-undecidable", "result": "no_action_needed",
                           "checked_at": "2026-09-18T00:00:00Z", "actor_kind": "person", "actor": "p"}})
    report = score_automation(_reader(rows), "ticket-triage", until=UNTIL)
    assert _classes(report)["prop-undecidable"] == "false"


def test_bad_window_timestamp_is_an_error():
    with pytest.raises(RecordError):
        score_automation(_reader(_rows()), "ticket-triage", since="yesterday")


def test_markdown_rendering_carries_the_headline_numbers():
    text = render_markdown(score_automation(_reader(_rows()), "ticket-triage", until=UNTIL))
    assert text.startswith("# Shadow report: ticket-triage")
    assert "Agreement: 0.6667" in text and "upper bound 0.3333" in text
    assert "| human | 4 | 1.4 h | 120.2 h |" in text


def test_readiness_helper_names_each_failing_threshold():
    report = score_automation(_reader(_rows()), "ticket-triage", until=UNTIL)
    verdict = promotion_readiness(report, min_proposals=30, min_agreement=0.8, max_false_rate=0.1)
    assert verdict["ready"] is False and len(verdict["reasons"]) == 3
    assert any("6 scored" in r for r in verdict["reasons"])
    assert any("agreement 0.67" in r for r in verdict["reasons"])
    assert any("0.33" in r for r in verdict["reasons"])

    ok = promotion_readiness(report, min_proposals=6, min_agreement=0.6, max_false_rate=0.4)
    assert ok == {"ready": True, "reasons": []}

    edge = promotion_readiness(report, min_proposals=6, min_agreement=0.6667, max_false_rate=0.3333)
    assert edge["ready"] is True  # thresholds are inclusive


def test_readiness_fails_when_rates_are_missing():
    verdict = promotion_readiness({"scored": 0, "agreement_rate": None, "false_proposal_rate_upper": None},
                                  min_proposals=0)
    assert verdict["ready"] is False and len(verdict["reasons"]) == 2


def test_cli_prints_json_and_markdown(monkeypatch, capsys):
    monkeypatch.setattr(shadow, "okg_reader", lambda deployment, dsn=None: _reader(_rows()))
    assert shadow.main(["--deployment", "d", "--automation", "ticket-triage", "--until", UNTIL]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["agree"] == 2
    assert shadow.main(["--deployment", "d", "--automation", "ticket-triage", "--until", UNTIL,
                        "--format", "markdown"]) == 0
    assert capsys.readouterr().out.startswith("# Shadow report")
