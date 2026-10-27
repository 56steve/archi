"""archi.tools.tickets: the L2 mediators of ticket-triage, against an in-memory ticket system.

Each tool checks the approval before any request, reads the ticket, changes nothing when the field already
holds the value or changed since the proposal, writes with a note naming the escalation, and reads back."""
import copy

import pytest

from archi.tools import tickets
from archi.tools._live import ActRefused, Approval, ToolError
from ticket_fakes import TOKEN, FakeTicketSystem

ENV = {"RM_TOKEN": TOKEN, "JIRA_TOKEN": TOKEN, "SN_TOKEN": TOKEN}
CONTEXT = {"idempotency_key": "ticket-triage~redmine-4820-x~1", "proposal_id": "prop-ticket-triage~redmine-4820-x~1",
           "workflow_id": "ticket-triage~redmine-4820-x~1", "dsn": "postgresql://fixture"}
APPROVAL = Approval("esc-abc", CONTEXT["proposal_id"], "operator-x")


def system():
    s = FakeTicketSystem()
    s.add("redmine", "4820", status="New", assignee="")
    s.add("redmine", "4811", status="New", assignee="Alan Turing")
    s.add("jira", "OPS-15", status="Open", assignee="")
    s.add("servicenow", "INC0010003", table="incident", state="New", assignment_group="")
    return s


def approved(context, payload, action_class):
    return APPROVAL


def endpoint(system_name):
    return {"redmine": {"url": "https://redmine.example.org", "token_env": "RM_TOKEN"},
            "jira": {"url": "https://jira.example.org", "token_env": "JIRA_TOKEN"},
            "servicenow": {"url": "https://example.service-now.com", "token_env": "SN_TOKEN"}}[system_name]


def assign(system_name, ticket, value, field="assignee", **extra):
    return {"system": system_name, "ticket": ticket, "field": field, "value": value, "expected": "",
            "note": "Assigned by the ticket-triage automation.", "endpoint": endpoint(system_name), **extra}


def status(system_name, ticket, to, was, **extra):
    return {"system": system_name, "ticket": ticket, "status": to, "expected_status": was,
            "note": "Triaged by the ticket-triage automation.", "endpoint": endpoint(system_name), **extra}


def run(tool, payload, s, verify=approved, env=ENV):
    return tool(payload=payload, context=CONTEXT, session=s.session(), env=env, verify=verify)


@pytest.mark.parametrize("payload, key, field, value", [
    (assign("redmine", "4820", "Grace Hopper"), ("redmine", "4820"), "assignee", "Grace Hopper"),
    (assign("jira", "OPS-15", "Grace Hopper"), ("jira", "OPS-15"), "assignee", "Grace Hopper"),
    (assign("servicenow", "INC0010003", "Cluster Operations", field="assignment_group", table="incident"),
     ("servicenow", "INC0010003"), "assignment_group", "Cluster Operations"),
])
def test_an_approved_assignment_is_written_with_a_note_naming_the_escalation(payload, key, field, value):
    s = system()
    out = run(tickets.assign_ticket, payload, s)
    assert out["status"] == "succeeded" and "esc-abc" in out["result"], out
    ticket = s.ticket(*key)
    assert ticket[field] == value
    (note,) = ticket["notes"]
    assert "Approved by operator-x in escalation esc-abc" in note and CONTEXT["proposal_id"] in note
    assert TOKEN not in str(out)


@pytest.mark.parametrize("payload, key, field", [
    (status("redmine", "4811", "In Progress", "New"), ("redmine", "4811"), "status"),
    (status("jira", "OPS-15", "In Progress", "Open"), ("jira", "OPS-15"), "status"),
    (status("servicenow", "INC0010003", "In Progress", "New", table="incident"), ("servicenow", "INC0010003"), "state"),
])
def test_an_approved_status_change_is_written_and_read_back(payload, key, field):
    s = system()
    out = run(tickets.set_ticket_status, payload, s)
    assert out["status"] == "succeeded", out
    assert s.ticket(*key)[field] == "In Progress" and len(s.ticket(*key)["notes"]) == 1
    assert [r[0] for r in s.requests][-1] == "GET"  # the read-back is the last request


def test_a_repeat_changes_nothing():
    s = system()
    run(tickets.assign_ticket, assign("redmine", "4820", "Grace Hopper"), s)
    writes = len(s.writes())
    out = run(tickets.assign_ticket, assign("redmine", "4820", "Grace Hopper"), s)
    assert out["status"] == "succeeded" and "already Grace Hopper; nothing changed" in out["result"]
    assert len(s.writes()) == writes and len(s.ticket("redmine", "4820")["notes"]) == 1


def test_a_stale_proposal_changes_nothing():
    s = system()
    s.ticket("redmine", "4820")["assignee"] = "Alan Turing"  # a person assigned it after the proposal
    out = run(tickets.assign_ticket, assign("redmine", "4820", "Grace Hopper"), s)
    assert out["status"] == "failed" and "stale" in out["result"] and s.writes() == []


def test_no_approval_means_no_request_at_all():
    s = system()
    with pytest.raises(ActRefused):
        run(tickets.assign_ticket, assign("redmine", "4820", "Grace Hopper"), s, verify=lambda c, p, a: None)
    with pytest.raises(ActRefused):
        tickets.assign_ticket(payload=assign("redmine", "4820", "Grace Hopper"), context={}, session=s.session(),
                              env=ENV, verify=approved)
    assert s.requests == []


def test_the_tool_checks_the_approval_against_this_class_and_payload():
    seen = []

    def verify(context, payload, action_class):
        seen.append((action_class, copy.deepcopy(payload)))
        return APPROVAL

    s = system()
    payload = assign("redmine", "4820", "Grace Hopper")
    run(tickets.assign_ticket, payload, s, verify=verify)
    assert seen == [("assign_ticket", payload)]


@pytest.mark.parametrize("change, fragment", [
    ({"endpoint": {"url": "http://redmine.example.org", "token_env": "RM_TOKEN"}}, "must be https"),
    ({"ticket": "OPS-15"}, "is not a redmine ticket key"),
    ({"table": "incident"}, "needs its table"),
    ({"field": "assignment_group"}, "assignment_group is a ServiceNow field"),
    ({"endpoint": {"url": "https://redmine.example.org", "token_env": "MISSING_TOKEN"}}, "MISSING_TOKEN"),
])
def test_bad_payloads_are_refused_before_any_request(change, fragment):
    s = system()
    with pytest.raises(ToolError, match=fragment):
        run(tickets.assign_ticket, {**assign("redmine", "4820", "Grace Hopper"), **change}, s)
    assert s.requests == []


def test_plain_http_is_accepted_only_for_a_loopback_host():
    s = system()
    payload = {**assign("redmine", "4820", "Grace Hopper"), "endpoint": {"url": "http://127.0.0.1:8099", "token_env": "RM_TOKEN"}}
    assert run(tickets.assign_ticket, payload, s)["status"] == "succeeded"


def test_a_write_with_no_answer_is_unknown_not_failed():
    s = system()
    s.fail_writes = True
    out = run(tickets.assign_ticket, assign("redmine", "4820", "Grace Hopper"), s)
    assert out["status"] == "unknown" and "may have applied" in out["result"]


def test_an_unknown_person_or_status_fails_without_a_write():
    s = system()
    out = run(tickets.assign_ticket, assign("redmine", "4820", "Nobody Known"), s)
    assert out["status"] == "failed" and "0 members named" in out["result"] and s.writes() == []
    out = run(tickets.set_ticket_status, status("jira", "OPS-15", "Closed Forever", "Open"), s)
    assert out["status"] == "failed" and "0 transitions" in out["result"] and s.writes() == []


def test_requests_never_follow_redirects():
    s = system()
    session = s.session()
    tickets.assign_ticket(payload=assign("redmine", "4820", "Grace Hopper"), context=CONTEXT, session=session,
                          env=ENV, verify=approved)
    assert session.calls and all(c["allow_redirects"] is False for c in session.calls)
