"""OpsIO's promotion lookup: `mode: live` counts only for a PACT change the ledger says a person approved."""
import json
import subprocess
from datetime import datetime, timezone

import pytest

from archi.ops import okg_io
from archi.ops.okg_io import OpsIO

APPROVED_AT = datetime(2026, 10, 25, 15, tzinfo=timezone.utc)


class Conn:
    def __init__(self, row):
        self.row, self.sql = row, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        return self

    def fetchone(self):
        return self.row


def io_with(monkeypatch, tmp_path, view=None, stdout=None, row=(APPROVED_AT,), returncode=0):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"], seen["cwd"] = cmd, kw["env"], kw["cwd"]
        out = stdout if stdout is not None else json.dumps({"derived_state": view})
        return subprocess.CompletedProcess(cmd, returncode, out, "")

    monkeypatch.setattr(okg_io.subprocess, "run", fake_run)
    io = OpsIO("postgresql://x", "dep", okg="okg")
    conn = Conn(row)
    monkeypatch.setattr(io, "connect", lambda: conn)
    return io, seen, conn


def test_an_approved_change_is_read_with_its_approval_time(monkeypatch, tmp_path):
    io, seen, conn = io_with(monkeypatch, tmp_path, {"state": "retired", "authoritative": True})
    status = io.promotion_status(tmp_path, "promote-x")
    assert status == {"approved": True, "at": APPROVED_AT.isoformat(), "reason": ""}
    assert seen["cmd"] == ["okg", "pact", "--root", str(tmp_path), "view", "promote-x", "--format", "status", "--json"]
    assert seen["env"]["OKG_PACT_GRAPH_DEPLOYMENT"] == "dep" and seen["env"]["OKG_DSN"] == "postgresql://x"
    assert seen["cwd"] == str(tmp_path)
    assert conn.sql[0][1] == ("promote-x",) and "verdict = 'approve'" in conn.sql[0][0]


@pytest.mark.parametrize("view, fragment", [
    ({"state": "awaiting_review", "authoritative": True}, "is awaiting_review; an operator approves it with `okg pact decide promote-x"),
    ({"state": "in_progress", "authoritative": True}, "is in_progress"),
    ({"state": "retired", "authoritative": False}, "the ledger was not consulted"),
])
def test_a_change_that_is_not_approved_says_how_to_approve_it(monkeypatch, tmp_path, view, fragment):
    io, _, _ = io_with(monkeypatch, tmp_path, view)
    status = io.promotion_status(tmp_path, "promote-x")
    assert status["approved"] is False and status["at"] is None and fragment in status["reason"]


def test_a_retired_change_with_no_approve_verdict_is_not_approved(monkeypatch, tmp_path):
    io, _, _ = io_with(monkeypatch, tmp_path, {"state": "retired", "authoritative": True}, row=None)
    status = io.promotion_status(tmp_path, "promote-x")
    assert status["approved"] is False and "no approve verdict" in status["reason"]


def test_an_unreadable_answer_is_not_approved(monkeypatch, tmp_path):
    io, _, _ = io_with(monkeypatch, tmp_path, stdout="not json", returncode=1)
    status = io.promotion_status(tmp_path, "promote-x")
    assert status["approved"] is False and "could not read the state of PACT change promote-x" in status["reason"]


def test_no_change_named_is_not_approved_and_asks_for_a_promotion(monkeypatch, tmp_path):
    io, seen, _ = io_with(monkeypatch, tmp_path, {})
    status = io.promotion_status(tmp_path, None)
    assert status["approved"] is False and "names no promotion change" in status["reason"] and "cmd" not in seen


def test_demotions_are_written_and_read_in_the_archi_ops_table(monkeypatch):
    io = OpsIO("postgresql://x", "dep")
    conn = Conn((APPROVED_AT, "demoted to shadow: x"))
    monkeypatch.setattr(io, "connect", lambda: conn)
    io.record_demotion("triage", "triage~s~1", APPROVED_AT, "demoted to shadow: x")
    assert "INSERT INTO archi_ops.demotions" in conn.sql[0][0] and "ON CONFLICT DO NOTHING" in conn.sql[0][0]
    assert io.latest_demotion("triage") == {"at": APPROVED_AT.isoformat(), "reason": "demoted to shadow: x"}
    conn.row = None
    assert io.latest_demotion("triage") is None


def test_the_tables_include_everything_the_runtime_writes():
    for table in ("signals", "halts", "mediations", "demotions"):
        assert f"archi_ops.{table}" in okg_io.SIGNALS_DDL
