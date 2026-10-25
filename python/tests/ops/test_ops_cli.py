"""The workflow-free pieces of the runtime and `archi ops`: the renderer's runtime line,
the instance configuration, reading an escalation row back, and the halt, resume,
status and answer logic over a fake OpsIO."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from archi.bundles import discovery
from archi.bundles.lock import render_and_lock
from archi.bundles.cli import build_parser
from archi.cli import ops
from archi.ops.control import RunInfo
from archi.ops.escalation import Decision, decision_from_row, may_execute, Escalation
from archi.ops.levels import LevelError, instance_levels
from archi.ops.runtime_config import RuntimeConfigError, load_runtime, raw_levels, read_settings

LIBRARY = Path(__file__).resolve().parent / "fixtures" / "library"
ALPHA = Path(__file__).resolve().parents[1] / "bundles" / "fixtures" / "library" / "packs" / "alpha"
T0 = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


@pytest.fixture
def instance(tmp_path):
    root = tmp_path / "lib"
    shutil.copytree(LIBRARY, root)
    shutil.copytree(ALPHA, root / "packs" / "alpha")
    bdir = root / "bundles" / "triage-team"
    bdir.mkdir(parents=True)
    (bdir / "bundle.yaml").write_text(yaml.safe_dump({
        "name": "triage-team", "version": "0.1.0", "description": "t",
        "packs": {"default": ["alpha", "fixture-triage"]}, "okg": ">=0.2.0,<0.3",
    }, sort_keys=False))
    catalog = discovery.from_roots([root / "packs"], [root / "bundles"])
    inst = tmp_path / "inst"
    render_and_lock(catalog, inst, ["triage-team"])
    return inst, catalog, root


# --- the renderer declares the runtime workflow -------------------------------------


def test_render_declares_the_ops_workflow_when_an_automation_is_selected(instance):
    inst, _, _ = instance
    defaults = yaml.safe_load(next(inst.glob("profiles/*/deployment-defaults.yaml")).read_text())
    assert defaults["runtime"] == {"enabled": True, "workflows": ["archi.ops.workflow"]}


def test_render_leaves_the_runtime_off_without_an_automation(tmp_path):
    root = tmp_path / "lib"
    shutil.copytree(LIBRARY, root)
    shutil.copytree(ALPHA, root / "packs" / "alpha")
    bdir = root / "bundles" / "plain"
    bdir.mkdir(parents=True)
    (bdir / "bundle.yaml").write_text(yaml.safe_dump({
        "name": "plain", "version": "0.1.0", "description": "t", "packs": {"default": ["alpha"]},
        "okg": ">=0.2.0,<0.3"}, sort_keys=False))
    catalog = discovery.from_roots([root / "packs"], [root / "bundles"])
    render_and_lock(catalog, tmp_path / "inst", ["plain"])
    defaults = yaml.safe_load(next((tmp_path / "inst").glob("profiles/*/deployment-defaults.yaml")).read_text())
    assert defaults["runtime"] == {"enabled": False}


# --- configuration ----------------------------------------------------------------------


def write_instance(inst: Path, levels=None, ops_yaml=None):
    (inst / "archi.yaml").write_text(yaml.safe_dump({"name": "dep", "automations": {
        n: {"level": lv} for n, lv in (levels or {}).items()}}))
    if ops_yaml is not None:
        (inst / "ops.yaml").write_text(yaml.safe_dump(ops_yaml))


def test_runtime_config_reads_levels_leniently_so_l3_reaches_the_run(instance):
    inst, _, _ = instance
    write_instance(inst, {"fixture-triage": "L3"}, {"automations": {"fixture-triage": {"mode": "live", "timeout_seconds": 30}}})
    assert raw_levels(inst) == {"fixture-triage": "L3"}
    rt = load_runtime({"ARCHI_INSTANCE": str(inst), "OKG_DSN": "postgresql://x"})
    assert rt.deployment == "dep"
    config = rt.automation("fixture-triage")
    assert (config.instance_level, config.mode, config.timeout_seconds) == ("L3", "live", 30)


def test_runtime_config_defaults_to_shadow_and_refuses_unknown_keys(instance, tmp_path):
    inst, _, _ = instance
    write_instance(inst, ops_yaml={"automations": {"a": {}}})
    assert read_settings(inst)["a"].mode == "shadow"
    write_instance(inst, ops_yaml={"automations": {"a": {"modee": "live"}}})
    with pytest.raises(RuntimeConfigError, match="unknown keys"):
        read_settings(inst)
    with pytest.raises(RuntimeConfigError, match="ARCHI_INSTANCE"):
        load_runtime({})
    with pytest.raises(RuntimeConfigError, match="OKG_DSN"):
        load_runtime({"ARCHI_INSTANCE": str(inst)})


def test_instance_levels():
    assert instance_levels({"a": {"level": "L1"}, "b": {}}) == {"a": "L1", "b": None}
    with pytest.raises(LevelError, match="L3"):
        instance_levels({"a": {"level": "L3"}})


# --- reading an escalation row back -----------------------------------------------------


def row(status="applied", decision=None, by="alice", at="2026-10-25T12:01:00+00:00"):
    return {"status": status, "decided_by": by, "decided_at": at,
            "decision": decision if decision is not None else {"verdict": "approve"}}


@pytest.mark.parametrize("attrs, status, error", [
    (row(), "approved", None),
    (row("decided", {"verdict": "reject", "note": "no"}), "rejected", None),
    (row("timed_out", {}), "timed_out", None),
    (row("pending", {}), "error", "pending, not decided"),
    (row("decided", {"verdict": "maybe"}), "error", "verdict must be one of"),
    (None, "error", "not found"),
])
def test_decision_from_row(attrs, status, error):
    d = decision_from_row("esc-1", "ops_proposal:prop-9", attrs)
    assert (d.status, d.proposal_id, d.escalation_id) == (status, "prop-9", "esc-1")
    assert (error is None) or error in d.error


def test_a_row_for_another_subject_cannot_execute():
    d = decision_from_row("esc-1", "jira:X-1", row())
    esc = Escalation("esc-1", "prop-9", T0, T0.replace(hour=13))
    assert d.proposal_id is None and not may_execute(d, esc, T0.replace(minute=30))
    good = decision_from_row("esc-1", "ops_proposal:prop-9", row())
    assert may_execute(good, esc, T0.replace(minute=30))


# --- archi ops --------------------------------------------------------------------------


class FakeIO:
    deployment, dsn = "dep", "postgresql://x"

    def __init__(self, schedules=None, runs=(), escalations=(), halts=()):
        self._schedules = dict(schedules if schedules is not None else {"archi_ops_scan": False})
        self._runs, self._escalations, self._halts = list(runs), list(escalations), list(halts)
        self.calls = []

    def ensure_tables(self): self.calls.append(("ensure",))
    def schedules(self): return dict(self._schedules)
    def runs(self, live_only=True): return list(self._runs)
    def run_input(self, wid): return {"id": "sig-1", "entity": "T2_US_MIT", "title": "Site down"}
    def pin_generation(self): return "gen-1"
    def read_nodes(self, subtype, generation, where=None): return []

    def pause(self, name):
        self.calls.append(("pause", name))
        self._schedules[name] = True

    def resume(self, name):
        self.calls.append(("resume", name))
        self._schedules[name] = False

    def record_halt(self, names, by): self.calls.append(("record_halt", tuple(names), by)); self._halts += list(names)
    def paused_by_halt(self): return list(self._halts)
    def clear_halt(self, name): self.calls.append(("clear_halt", name)); self._halts.remove(name)
    def cancel(self, wid): self.calls.append(("cancel", wid))

    def escalations(self, open_only=True, workflow_ids=None):
        return [e for e in self._escalations if workflow_ids is None or e["requested_by_workflow"] in workflow_ids]

    def respond(self, escalation_id, verdict, *, note, decided_by):
        self.calls.append(("respond", escalation_id, verdict, note, decided_by))

    def day_spend(self, automation): return (3.0, 10.0)


def test_halt_pauses_then_closes_escalations_then_cancels_and_records():
    io = FakeIO(
        runs=[RunInfo("r1", "fixture-triage", "PENDING"), RunInfo("r2", "fixture-triage", "SUCCESS")],
        escalations=[{"escalation_id": "esc-1", "proposal_id": "prop-r1", "requested_by_workflow": "r1",
                      "timeout_at": "x", "reason": "r"}])
    submitted = []
    out = ops.do_halt(io, who="jason", log=lambda _: None,
                      submit_records=lambda records, **kw: submitted.append((records, kw)))
    assert [c[0] for c in io.calls] == ["ensure", "pause", "record_halt", "respond", "cancel"]
    assert io.calls[3] == ("respond", "esc-1", "reject", "closed by archi ops halt", "archi ops halt")
    assert out == {"paused": ["archi_ops_scan"], "already_paused": [], "closed": ["esc-1"],
                   "cancelled": ["r1"], "recorded": ["r1"], "left_final": ["r2"]}
    records, kw = submitted[0]
    verdicts = {n["attrs"]["record_id"]: n["attrs"].get("verdict") for n in records.nodes.values()}
    assert verdicts["dec-r1-halt"] == "halted"
    assert records.nodes["ops_proposal:prop-r1-halt"]["attrs"]["mode"] == "placeholder"
    assert records.dangling() == [] and kw["idempotency_key"] == "r1|halt"


def test_halt_survives_an_escalation_that_is_already_closed():
    class Closed(FakeIO):
        def respond(self, *a, **k):
            raise RuntimeError("is in status 'applied'")

    io = Closed(runs=[RunInfo("r1", "a", "PENDING")], escalations=[
        {"escalation_id": "esc-1", "proposal_id": "p", "requested_by_workflow": "r1", "timeout_at": "x", "reason": ""}])
    out = ops.do_halt(io, log=lambda _: None, submit_records=lambda *a, **k: None)
    assert out["closed"] == [] and out["cancelled"] == ["r1"]


def test_resume_only_resumes_what_halt_paused():
    io = FakeIO(schedules={"archi_ops_scan": True}, halts=["archi_ops_scan"])
    assert ops.do_resume(io, log=lambda _: None)["resumed"] == ["archi_ops_scan"]
    assert ("clear_halt", "archi_ops_scan") in io.calls
    operator_paused = FakeIO(schedules={"archi_ops_scan": True}, halts=[])
    assert ops.do_resume(operator_paused, log=lambda _: None)["resumed"] == []


def test_respond_passes_the_verdict_and_the_decider():
    io = FakeIO()
    assert ops.do_respond(io, "esc-1", "approve", "ok", "alice")["verdict"] == "approve"
    assert io.calls == [("respond", "esc-1", "approve", "ok", "alice")]


def test_status_reports_levels_schedules_budgets_and_pending(instance):
    inst, _, _ = instance
    write_instance(inst, {"fixture-triage": "L3"}, {"automations": {"fixture-triage": {"mode": "live"}}})
    io = FakeIO(schedules={"archi_ops_scan": True}, escalations=[
        {"escalation_id": "esc-1", "proposal_id": "p", "requested_by_workflow": "r", "timeout_at": "t", "reason": ""}])
    report = ops.status_report(inst, io, proposals=[{"attrs": {"automation": "fixture-triage", "mode": "shadow"}}],
                               shadow=lambda name: {"incidents": 0})
    assert report["scanner"] == {"archi_ops_scan": "paused"}
    (row,) = report["automations"]
    assert row["name"] == "fixture-triage" and row["level"] is None and "L3" in row["level_problem"]
    assert row["mode"] == "live" and row["schedule_paused"] is False  # keyed by automation name, not the scanner
    assert row["budget"]["day_spent"] == 3.0 and row["budget"]["per_day_cap"] == 10.0
    assert row["shadow"]["shadow_proposals"] == 1 and row["shadow_report"] == {"incidents": 0}
    assert len(report["pending"]) == 1
    json.dumps(report, default=str)


def test_status_says_so_when_the_shadow_report_cannot_be_built(instance):
    inst, _, _ = instance
    write_instance(inst, {"fixture-triage": "L1"})

    def broken(name):
        raise RuntimeError("no records yet")

    report = ops.status_report(inst, FakeIO(), shadow=broken)
    assert report["automations"][0]["level"] == "L1"
    assert report["automations"][0]["shadow_report"] == {"error": "RuntimeError: no records yet"}


def test_the_ops_commands_are_registered():
    parser = build_parser()
    for argv, command in (
        (["ops", "status", "--json"], "status"), (["ops", "halt"], "halt"), (["ops", "resume"], "resume"),
        (["ops", "pending"], "pending"), (["ops", "approve", "esc-1", "--note", "n", "--as", "bob"], "approve"),
        (["ops", "reject", "esc-1"], "reject"),
    ):
        args = parser.parse_args(argv)
        assert args.command == "ops" and args.command_name == command
    approve = parser.parse_args(["ops", "approve", "esc-1", "--as", "bob", "--note", "n"])
    assert (approve.escalation_id, approve.as_user, approve.note) == ("esc-1", "bob", "n")


def test_dsn_comes_from_the_flag_then_the_environment(monkeypatch, tmp_path):
    parser = build_parser()
    monkeypatch.setenv("OKG_DSN", "postgresql://env")
    assert ops.resolve_dsn(parser.parse_args(["ops", "--dsn", "postgresql://flag", "pending"])) == "postgresql://flag"
    assert ops.resolve_dsn(parser.parse_args(["ops", "pending"])) == "postgresql://env"
    monkeypatch.delenv("OKG_DSN")
    with pytest.raises(ops.OpsCliError, match="no database"):
        ops.resolve_dsn(parser.parse_args(["ops", "--instance", str(tmp_path), "pending"]))


def test_submit_passes_the_idempotency_key(monkeypatch):
    from archi.ops import record

    seen = {}

    class Done:
        returncode, stdout, stderr = 0, json.dumps({"output": {"status": "accepted"}}), ""

    monkeypatch.setattr(record.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and Done())
    records = record.OpsRecords()
    records.add("incident", record_id="i1", title="t", entity="e")
    record.submit(records, deployment="dep", idempotency_key="wf|record-1")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--idempotency-key") + 1] == "wf|record-1"
