"""The tickets downloader's JIRA reader (and Redmine beside it) over a fake HTTP session.

task.r3-bundle-cluster-manager.tickets. Recorded JIRA REST v2 search pages on
example.org hosts; no request leaves the process and no live JIRA is involved.
"""
import json

import pytest
import yaml

from archi.downloaders import tickets
from archi.sources.tickets import TicketSource
from packs.cluster_fixtures import JIRA, FakeResponse, FakeSession, jira_session, load
from packs.pack_support import PACKS_DIR

TOKEN = {"JIRA_API_TOKEN": "token-fixture"}
REDMINE = "https://redmine.example.org"


def _records(path):
    return json.loads((path / "records.json").read_text())


def test_jira_pages_by_start_at_until_the_total_even_when_jira_shortens_the_page(tmp_path):
    # Ask for 50 a page; the server caps it at 2, so a short page must not end the run.
    session = jira_session()
    info = tickets.download(session, TOKEN, tmp_path, jira_url=JIRA + "/", jira_projects=["OPS"], page_size=50)
    assert info["record_count"] == 3
    assert [r[1] for r in session.requests] == [f"{JIRA}/rest/api/2/search"] * 2
    params = [r[2]["params"] for r in session.requests]
    assert [p["startAt"] for p in params] == [0, 2] and params[0]["maxResults"] == 50
    assert params[0]["jql"] == "project in (OPS) ORDER BY updated DESC"
    assert "comment" in params[0]["fields"].split(",")
    assert all(r[2]["headers"]["Authorization"] == "Bearer token-fixture" for r in session.requests)
    assert info["sources"] == [{"system": "jira", "base_url": JIRA, "jql": params[0]["jql"], "authenticated": True}]


def test_an_empty_page_ends_the_paging(tmp_path):
    pages = iter([load("jira_search_page1.json"), {"startAt": 2, "total": 9, "issues": []}])
    session = FakeSession(gets=[lambda url, kw: FakeResponse(200, next(pages))])
    assert tickets.download(session, TOKEN, tmp_path, jira_url=JIRA)["record_count"] == 2
    assert len(session.requests) == 2


def test_jira_issues_map_onto_the_flat_ticket_shape(tmp_path):
    tickets.download(jira_session(), TOKEN, tmp_path, jira_url=JIRA, jira_projects=["OPS"])
    by_key = {r["key"]: r for r in _records(tmp_path)}
    assert set(by_key) == {"OPS-12", "OPS-9", "OPS-3"}
    one = by_key["OPS-12"]
    assert one["system"] == "jira" and one["url"] == f"{JIRA}/browse/OPS-12"
    assert (one["subject"], one["project"], one["tracker"], one["status"], one["priority"]) == (
        "GPU node gpu07 drops out of the pool", "Cluster Operations", "Incident", "In Progress", "High")
    assert (one["author"], one["assignee"]) == ("Katherine Johnson", "Dorothy Vaughan")
    assert (one["created"], one["updated"], one["closed"]) == (
        "2026-10-14T07:02:11.000+0000", "2026-10-14T11:20:45.000+0000", "")
    assert one["notes"][0] == {"author": "Dorothy Vaughan", "created": "2026-10-14T11:20:45.000+0000",
                               "body": "Reseated the GPU; watching it overnight."}
    closed = by_key["OPS-9"]
    assert closed["closed"] == "2026-10-06T16:45:00.000+0000"
    assert closed["assignee"] == "" and closed["description"] == "" and closed["notes"] == []


def test_people_are_written_as_display_names_never_logins_or_addresses(tmp_path):
    tickets.download(jira_session(), TOKEN, tmp_path, jira_url=JIRA)
    written = (tmp_path / "records.json").read_text()
    for login in ("kjohnson", "dvaughan", "mhamilton", "dorothy.vaughan@example.org"):
        assert login not in written


def test_a_missing_token_is_refused_by_name_before_any_request(tmp_path):
    session = jira_session()
    with pytest.raises(tickets.DownloadError, match="JIRA_API_TOKEN is not set"):
        tickets.download(session, {}, tmp_path, jira_url=JIRA, jira_projects=["OPS"])
    assert session.requests == [] and not list(tmp_path.iterdir())


def test_a_missing_token_stops_a_two_tracker_run_before_redmine_is_read(tmp_path):
    session = FakeSession()
    with pytest.raises(tickets.DownloadError, match="JIRA_API_TOKEN"):
        tickets.download(session, {}, tmp_path, redmine_url=REDMINE, jira_url=JIRA)
    assert session.requests == []


def test_the_token_is_never_written_or_printed(tmp_path, capsys):
    out = tmp_path / "tickets"
    code = tickets.main(["--jira-url", JIRA, "--jira-project", "OPS", "--output-dir", str(out)],
                        session=jira_session(), env=TOKEN)
    assert code == 0
    printed = capsys.readouterr()
    assert "token-fixture" not in printed.out + printed.err
    assert "token-fixture" not in "".join(p.read_text() for p in out.iterdir())
    assert json.loads(printed.out)["record_count"] == 3


@pytest.mark.parametrize("status, words", [(401, "JIRA_API_TOKEN was rejected"), (400, "JQL was refused"), (500, "HTTP 500")])
def test_a_refused_search_stops_the_run_and_writes_nothing(tmp_path, status, words):
    session = FakeSession(gets=[lambda url, kw: FakeResponse(status, {"errorMessages": ["no"]})])
    with pytest.raises(tickets.DownloadError, match=words):
        tickets.download(session, TOKEN, tmp_path, jira_url=JIRA)
    assert not list(tmp_path.iterdir())


def test_a_jira_url_that_is_not_https_is_refused_before_any_request(tmp_path):
    session = jira_session("http://jira.example.org")
    with pytest.raises(tickets.DownloadError, match="https"):
        tickets.download(session, TOKEN, tmp_path, jira_url="http://jira.example.org")
    assert session.requests == []


def test_jql_is_passed_as_given_and_cannot_be_mixed_with_projects(tmp_path):
    session = jira_session()
    tickets.download(session, TOKEN, tmp_path, jira_url=JIRA, jql="project = OPS AND component = batch")
    assert session.requests[0][2]["params"]["jql"] == "project = OPS AND component = batch"
    with pytest.raises(tickets.DownloadError, match="not both"):
        tickets.download(jira_session(), TOKEN, tmp_path, jira_url=JIRA, jira_projects=["OPS"], jql="x")


@pytest.mark.parametrize("key", ["ops", "OPS) OR (1=1", "OPS,ADMIN", ""])
def test_a_project_that_is_not_a_project_key_is_refused(tmp_path, key):
    session = jira_session()
    with pytest.raises(tickets.DownloadError, match="project key"):
        tickets.download(session, TOKEN, tmp_path, jira_url=JIRA, jira_projects=[key])
    assert session.requests == []


def test_the_cap_limits_the_jira_read(tmp_path):
    session = jira_session()
    info = tickets.download(session, TOKEN, tmp_path, jira_url=JIRA, page_size=2, max_issues=2)
    assert info["record_count"] == 2 and len(session.requests) == 1


def test_no_issues_is_not_written_as_an_empty_cache(tmp_path):
    session = FakeSession(gets=[lambda url, kw: FakeResponse(200, {"startAt": 0, "total": 0, "issues": []})])
    with pytest.raises(tickets.DownloadError, match="no issues"):
        tickets.download(session, TOKEN, tmp_path, jira_url=JIRA)
    assert not list(tmp_path.iterdir())


def _both_session():
    session = jira_session()

    def issues(url, kwargs):
        if url != f"{REDMINE}/issues.json":
            return None
        return FakeResponse(200, load("redmine_issues_page1.json" if kwargs["params"]["offset"] == 0 else "redmine_issues_page2.json"))

    session.gets.append(issues)
    return session


def test_both_trackers_fill_one_cache_and_the_reader_keeps_them_apart(tmp_path):
    out = tmp_path / "tickets"
    info = tickets.download(_both_session(), TOKEN, out, redmine_url=REDMINE, jira_url=JIRA, page_size=2)
    assert info["record_count"] == 6 and [s["system"] for s in info["sources"]] == ["redmine", "jira"]

    run = TicketSource(records_path=str(out / "records.json"), meta_path=str(out / "meta.json")).run(
        "r", mode="scope_complete")
    assert run.completed_scope is True and run.health.record_count == 6
    nodes = {f.node_id: f.attrs for f in run.facts if getattr(f, "subtype", None) == "tracker_ticket"}
    assert {"tracker_ticket:redmine:4812", "tracker_ticket:jira:OPS-12"} <= set(nodes)
    # Each link comes from its own tracker's root, not the one base_url the pack was given.
    assert nodes["tracker_ticket:redmine:4812"]["url"] == f"{REDMINE}/issues/4812"
    assert nodes["tracker_ticket:jira:OPS-12"]["url"] == f"{JIRA}/browse/OPS-12"


def test_redmine_alone_reads_no_jira_and_needs_no_token(tmp_path):
    session = _both_session()
    info = tickets.download(session, {}, tmp_path, redmine_url=REDMINE, page_size=2)
    assert info["record_count"] == 3 and all("/issues.json" in r[1] for r in session.requests)
    assert {r["system"] for r in _records(tmp_path)} == {"redmine"}


def test_no_tracker_is_refused(tmp_path):
    with pytest.raises(tickets.DownloadError, match="--redmine-url, --jira-url"):
        tickets.download(FakeSession(), TOKEN, tmp_path)
    assert tickets.main(["--output-dir", str(tmp_path)], session=FakeSession(), env={}) == 1


def test_redmine_stays_the_default_tracker():
    questions = {q["id"]: q for q in yaml.safe_load((PACKS_DIR / "tickets" / "questions.yaml").read_text())["init_questions"]}
    assert questions["system"]["default"] == "redmine"
    assert TicketSource().system == "redmine"
    assert TicketSource().credential_refs == ("REDMINE_API_KEY",)
