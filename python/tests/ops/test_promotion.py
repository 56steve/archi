"""req.r3-ops-runtime.shadow: promotion to live needs a passing shadow report and an operator's approval
recorded as a PACT decision in the instance repo."""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from archi.ops.promotion import PromotionRefused, change_id, promote
from archi.ops.shadow import promotion_readiness

NOW = datetime(2026, 10, 25, 14, 30, 5, tzinfo=timezone.utc)
GOOD = {"scored": 40, "answered": 30, "response_coverage": 0.75, "agreement_rate": 0.9, "false_proposal_rate_upper": 0.05}
BAD = {"scored": 3, "answered": 1, "response_coverage": 0.33, "agreement_rate": 0.5, "false_proposal_rate_upper": 0.4}


def git(directory, *args):
    return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "ops@example.test")
    git(tmp_path, "config", "user.name", "ops")
    (tmp_path / "ops.yaml").write_text(yaml.safe_dump({"automations": {"triage": {"mode": "shadow", "timeout_seconds": 60}}}))
    (tmp_path / "other.txt").write_text("unrelated\n")
    git(tmp_path, "add", "ops.yaml", "other.txt")
    git(tmp_path, "commit", "-q", "-m", "baseline")
    return tmp_path


class FakeOkg:
    """Stands in for `okg pact new` / `okg pact decide`; `fail` names a subcommand that exits 1."""

    def __init__(self, root: Path, fail=None):
        self.root, self.fail, self.calls, self.envs = root, fail, [], []

    def __call__(self, cmd, **kw):
        if cmd[0] == "git":
            return subprocess.run(cmd, **kw)
        self.calls.append(cmd)
        self.envs.append(kw.get("env") or {})
        verb = cmd[cmd.index("--root") + 2]
        if verb == self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", f"{verb} refused")
        if verb == "new":
            change_dir = self.root / "pact" / "changes" / cmd[cmd.index("new") + 1]
            change_dir.mkdir(parents=True)
            (change_dir / "pact.yaml").write_text("id: x\n")
        return subprocess.CompletedProcess(cmd, 0, "{}", "")


def run(repo, report=GOOD, fail=None, **kw):
    okg = FakeOkg(repo, fail)
    result = promote(repo, "triage", report=report, readiness=promotion_readiness(report), principal="mohoney",
                     now=NOW, run=okg, okg="okg", dsn="postgresql://x", deployment="dep", **kw)
    return result, okg


def test_a_passing_report_records_the_decision_sets_live_and_commits_by_name(repo):
    result, okg = run(repo, note="reviewed the 40 incidents")
    change = "promote-triage-20261025t143005z"
    assert result["change"] == change == change_id("triage", NOW)
    new, ingest, gate, decide = okg.calls
    assert new[:5] == ["okg", "pact", "--root", str(repo), "new"] and new[5] == change
    assert ingest[4:7] == ["evidence", "ingest", change]
    assert ingest[ingest.index("--pytest-report") + 1] == f"pact/changes/{change}/readiness.xml"
    assert gate[4:7] == ["gate", "task-done", change] and gate[7] == f"task.{change}"
    assert decide[decide.index("decide") + 1] == change
    assert decide[decide.index("--verdict") + 1] == "approve" and decide[decide.index("--principal") + 1] == "mohoney"
    assert "scored=40" in decide[decide.index("--note") + 1] and "reviewed the 40 incidents" in decide[decide.index("--note") + 1]
    assert decide[decide.index("--dsn") + 1] == "postgresql://x"
    assert okg.envs[-1]["OKG_PACT_GRAPH_DEPLOYMENT"] == "dep" and okg.envs[-1]["OKG_DSN"] == "postgresql://x"
    ops = yaml.safe_load((repo / "ops.yaml").read_text())["automations"]["triage"]
    assert ops == {"mode": "live", "timeout_seconds": 60, "promoted_at": "2026-10-25T14:30:05Z"}
    assert json.loads((repo / "pact" / "changes" / change / "shadow-report.json").read_text())["scored"] == 40
    assert git(repo, "log", "-1", "--format=%s").strip() == f"promote triage to live ({change})"
    assert sorted(git(repo, "show", "--name-only", "--format=", "HEAD").split()) == sorted(
        ["ops.yaml", f"pact/changes/{change}/pact.yaml", f"pact/changes/{change}/shadow-report.json",
         f"pact/changes/{change}/readiness.xml"])
    assert git(repo, "status", "--porcelain").strip() == ""


def test_a_failing_report_is_refused_and_nothing_is_written(repo):
    before = git(repo, "rev-parse", "HEAD")
    okg = FakeOkg(repo)
    with pytest.raises(PromotionRefused, match="not ready to promote: only 3 scored incidents; need 30"):
        promote(repo, "triage", report=BAD, readiness=promotion_readiness(BAD), principal="mohoney", now=NOW,
                run=okg, okg="okg")
    assert okg.calls == [] and git(repo, "rev-parse", "HEAD") == before
    assert yaml.safe_load((repo / "ops.yaml").read_text())["automations"]["triage"]["mode"] == "shadow"
    assert not (repo / "pact").exists()


def test_an_empty_report_is_refused():
    with pytest.raises(PromotionRefused, match="not ready"):
        promote(Path("."), "triage", report={}, readiness=promotion_readiness({}), principal="x", now=NOW,
                run=FakeOkg(Path(".")), okg="okg")


def test_a_directory_that_is_not_a_git_repository_is_refused(tmp_path):
    (tmp_path / "ops.yaml").write_text(yaml.safe_dump({"automations": {"triage": {}}}))
    okg = FakeOkg(tmp_path)
    with pytest.raises(PromotionRefused, match="not a git repository"):
        promote(tmp_path, "triage", report=GOOD, readiness=promotion_readiness(GOOD), principal="x", now=NOW,
                run=okg, okg="okg")
    assert okg.calls == [] and "live" not in (tmp_path / "ops.yaml").read_text()


def test_an_automation_ops_yaml_does_not_know_is_refused(repo):
    with pytest.raises(PromotionRefused, match="no automations.nope"):
        promote(repo, "nope", report=GOOD, readiness=promotion_readiness(GOOD), principal="x", now=NOW,
                run=FakeOkg(repo), okg="okg")


@pytest.mark.parametrize("fail, what", [("new", "new"), ("evidence", "evidence ingest"), ("gate", "gate task-done"),
                                        ("decide", "decide")])
def test_when_okg_pact_fails_the_automation_stays_in_shadow_and_nothing_is_committed(repo, fail, what):
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(PromotionRefused, match=f"okg pact {what} failed"):
        run(repo, fail=fail)
    assert git(repo, "rev-parse", "HEAD") == before
    assert yaml.safe_load((repo / "ops.yaml").read_text())["automations"]["triage"]["mode"] == "shadow"


def test_the_cli_refuses_without_a_passing_report(monkeypatch, capsys, repo):
    from archi.bundles.cli import build_parser
    from archi.cli import ops
    from archi.ops import shadow

    monkeypatch.setattr(ops, "make_io", lambda args: type("Io", (), {"deployment": "dep", "dsn": "x"})())
    monkeypatch.setattr(shadow, "okg_source", lambda *a, **k: None)
    monkeypatch.setattr(shadow, "score_automation", lambda *a, **k: BAD)
    args = build_parser().parse_args(["ops", "--instance", str(repo), "promote", "triage"])
    assert args.func(args) == 2
    assert "not ready to promote" in capsys.readouterr().err
    assert yaml.safe_load((repo / "ops.yaml").read_text())["automations"]["triage"]["mode"] == "shadow"
