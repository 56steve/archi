"""req.r3-bundle-cluster-manager.live: the Slurm live tools, on a fake scheduler.

acc.r3-bundle-cluster-manager.gated: an act call without an approved escalation is refused. Every test
here runs the tools against `FakeScheduler` (a runner that keeps node and job state in a dict) or a
plain `FakeRunner`; `no_process` makes any real process start fail the test, so nothing here can
reach a scheduler. The real `subprocess_runner` is never called.
"""
import functools
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from archi.tools import _live, slurm
from archi.tools._live import ActRefused, Approval, RunResult, ToolError
from packs.cluster_fixtures import FakeRunner, text

REPO = Path(__file__).resolve().parents[3]
PACK = REPO / "bundles" / "packs" / "slurm"
CONTEXT = {"dsn": "postgresql://unused", "proposal_id": "prop-1", "idempotency_key": "run-77", "workflow_id": "wf-1"}


@pytest.fixture(autouse=True)
def no_process(monkeypatch):
    """Starting any process fails the test: the tools are only ever driven through a fake runner."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"a real process was started: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


def approved(context):
    return Approval("esc-1", context["proposal_id"], "alice")


class FakeScheduler:
    """A runner holding node and job state, answering the commands the tools build."""

    def __init__(self, nodes=None, jobs=None):
        self.nodes = {k: list(v) for k, v in (nodes or {}).items()}  # name -> [state, reason]
        self.jobs = dict(jobs or {})  # id -> state
        self.calls = []

    def __call__(self, argv, *, timeout):
        self.calls.append(list(argv))
        program = argv[0]
        if program == "sinfo":
            name = argv[argv.index("--nodes") + 1]
            if name not in self.nodes:
                return RunResult(0, "")
            state, reason = self.nodes[name]
            return RunResult(0, f"{name}|cpu|{state}|64|256000|{reason or 'none'}\n")
        if program == "squeue":
            job = argv[argv.index("--jobs") + 1]
            if job not in self.jobs:
                return RunResult(1, "", "slurm_load_jobs error: Invalid job id specified")
            return RunResult(0, f"{job}|cpu|name|ada|{self.jobs[job]}|1:00|1|node001\n")
        if program == "scontrol" and argv[1] == "update":
            fields = dict(part.split("=", 1) for part in argv[2:])
            name = fields["NodeName"]
            if fields.get("State") == "DRAIN":
                self.nodes[name] = ["drained", fields["Reason"]]
            elif fields.get("State") == "RESUME":
                self.nodes[name] = ["idle", ""]
            return RunResult(0)
        if program == "scontrol" and argv[1] == "requeue":
            self.jobs[argv[2]] = "PENDING"
            return RunResult(0)
        raise AssertionError(f"unexpected command {argv}")

    def writes(self):
        return [c for c in self.calls if c[0] == "scontrol"]


# --- read tools ------------------------------------------------------------------------

#: what `sinfo --Node --format=%N|%P|%T|%c|%m|%E` prints: six fields, the reason last
SINFO_OUT = "".join(f"{row}\n" for row in (
    "node001|cpu|mixed|64|256000|none", "node002|cpu|allocated|64|256000|none", "node003|cpu|idle|64|256000|none",
    "node042|cpu|drained|64|256000|archi[run-77]: scratch full [root@2026-10-12T09:41:00]",
    "gpu01|gpu|mixed|32|512000|none", "gpu02|gpu|down*|32|512000|Not responding [slurm@2026-10-13T06:00:00]",
    "node003|debug|idle|64|256000|none",
))


def test_squeue_builds_one_read_only_command_and_labels_the_payload_live():
    runner = FakeRunner().on(lambda a: a[0] == "squeue", text("squeue.out"))
    out = slurm.archi_slurm_squeue(user="ada", partition="gpu", state="running", job_id="1001", runner=runner)
    assert runner.calls == [[
        "squeue", "--noheader", "--format=%i|%P|%j|%u|%T|%M|%D|%R",
        "--user", "ada", "--partition", "gpu", "--states", "RUNNING", "--jobs", "1001",
    ]]
    assert out["boundary"] == "external_live" and out["observed_at"] and out["command"] == runner.calls[0]
    assert out["count"] == 3 and out["truncated"] is False
    first = out["rows"][0]
    assert first == {"job_id": "1001", "partition": "gpu", "name": "train_model", "user": "ada", "state": "RUNNING",
                     "elapsed": "1:20:00", "nodes": "1", "reason_or_nodelist": "gpu01"}
    assert out["rows"][1]["reason_or_nodelist"] == "(Resources)"


def test_a_job_name_holding_the_separator_stays_one_field():
    runner = FakeRunner().on(lambda a: True, text("squeue.out"))
    rows = slurm.archi_slurm_squeue(runner=runner)["rows"]
    assert rows[2]["name"] == "weird|name" and rows[2]["user"] == "alan" and rows[2]["reason_or_nodelist"] == "node[001-002]"


def test_sinfo_returns_nodes_with_state_and_drain_reason():
    runner = FakeRunner().on(lambda a: a[0] == "sinfo", SINFO_OUT)
    out = slurm.archi_slurm_sinfo(partition="cpu", runner=runner)
    assert runner.calls[0][:3] == ["sinfo", "--noheader", "--Node"] and "--partition" in runner.calls[0]
    drained = [r for r in out["rows"] if r["node"] == "node042"][0]
    assert drained["state"] == "drained" and drained["reason"].startswith("archi[run-77]")
    assert out["boundary"] == "external_live"


def test_results_are_bounded_and_say_so():
    runner = FakeRunner().on(lambda a: True, SINFO_OUT)
    out = slurm.archi_slurm_sinfo(max_results=2, runner=runner)
    assert len(out["rows"]) == 2 and out["count"] == 7 and out["truncated"] is True
    assert len(slurm.archi_slurm_sinfo(max_results=10**6, runner=runner)["rows"]) == 7
    assert len(slurm.archi_slurm_sinfo(max_results="x", runner=runner)["rows"]) == 7


@pytest.mark.parametrize("tool, kwargs", [
    (slurm.archi_slurm_squeue, {"user": "--all"}),
    (slurm.archi_slurm_squeue, {"partition": "gpu; rm -rf /"}),
    (slurm.archi_slurm_squeue, {"job_id": "12 13"}),
    (slurm.archi_slurm_squeue, {"state": "RUNNING,$(x)"}),
    (slurm.archi_slurm_sinfo, {"node": "-w evil"}),
    (slurm.archi_slurm_sinfo, {"partition": "a b"}),
])
def test_an_argument_that_is_not_a_plain_name_never_reaches_a_command(tool, kwargs):
    runner = FakeRunner()
    out = tool(runner=runner, **kwargs)
    assert "error" in out and out["boundary"] == "external_live"
    assert runner.calls == []


def test_a_failing_or_missing_command_comes_back_as_an_error_payload():
    failing = FakeRunner().on(lambda a: True, "", returncode=1, stderr="slurm_load_jobs error: Unable to contact slurm controller")
    assert "Unable to contact" in slurm.archi_slurm_squeue(runner=failing)["error"]

    def missing(argv, *, timeout):
        raise FileNotFoundError(2, "No such file or directory: 'squeue'")

    assert "FileNotFoundError" in slurm.archi_slurm_squeue(runner=missing)["error"]

    def slow(argv, *, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert "TimeoutExpired" in slurm.archi_slurm_sinfo(runner=slow)["error"]


# --- the gate: an act call without an approved escalation is refused ----------------------

ACTS = [
    ("drain_node", {"node": "node042", "reason": "scratch full"}),
    ("resume_node", {"node": "node042"}),
    ("requeue_job", {"job_id": "1001"}),
]


DRAIN_001 = {"node": "node001", "reason": "scratch full"}


def _scheduler():
    return FakeScheduler(nodes={"node042": ["drained", "x"], "node001": ["mixed", ""]}, jobs={"1001": "RUNNING"})


@pytest.mark.parametrize("name, payload", ACTS)
@pytest.mark.parametrize("context, why", [
    (None, "no call context"),
    ({}, "lacks idempotency_key, proposal_id, workflow_id"),
    ({k: v for k, v in CONTEXT.items() if k != "idempotency_key"}, "lacks idempotency_key"),
    ({k: v for k, v in CONTEXT.items() if k != "workflow_id"}, "lacks workflow_id"),
])
def test_an_act_without_the_runtime_context_is_refused_and_runs_nothing(name, payload, context, why):
    scheduler = _scheduler()
    with pytest.raises(ActRefused, match=why):
        getattr(slurm, name)(payload=payload, context=context, runner=scheduler, verify=lambda ctx: pytest.fail("asked"))
    assert scheduler.calls == []


@pytest.mark.parametrize("name, payload", ACTS)
@pytest.mark.parametrize("verify, why", [
    (lambda ctx: None, "no approved escalation"),
    (lambda ctx: Approval("esc-1", "prop-OTHER", "alice"), "not prop-1"),
    (lambda ctx: (_ for _ in ()).throw(RuntimeError("database unreachable")), "database unreachable"),
])
def test_an_act_without_an_approved_decision_is_refused_and_runs_nothing(name, payload, verify, why):
    scheduler = _scheduler()
    with pytest.raises(ActRefused, match=why):
        getattr(slurm, name)(payload=payload, context=CONTEXT, runner=scheduler, verify=verify)
    assert scheduler.calls == [], "not even a read: nothing is run before the approval is shown"


@pytest.mark.parametrize("name, payload", ACTS)
def test_the_refusal_comes_before_the_payload_is_looked_at(name, payload):
    with pytest.raises(ActRefused):
        getattr(slurm, name)(payload={"bogus": 1}, context=CONTEXT, runner=_scheduler(), verify=lambda ctx: None)


class FakeEscalations:
    """Just `decision_for`: the okg escalation row as `ops_verifier` reads it."""

    def __init__(self, status="applied", verdict="approve", by="alice", subject="ops_proposal:prop-1"):
        self.row = (subject, {"status": status, "decision": {"verdict": verdict}, "decided_by": by,
                              "decided_at": datetime(2026, 10, 25, 12, 1, tzinfo=timezone.utc).isoformat()})
        self.asked = []

    def decision_for(self, escalation_id):
        from archi.ops.escalation import decision_from_row

        self.asked.append(escalation_id)
        return decision_from_row(escalation_id, *self.row)


@pytest.mark.parametrize("kwargs, ok", [
    ({}, True),
    ({"verdict": "reject"}, False),
    ({"status": "timed_out"}, False),
    ({"status": "pending"}, False),
    ({"by": None}, False),
    ({"subject": "ops_proposal:prop-9"}, False),
])
def test_the_default_check_reads_the_run_s_escalation_row_from_okg(kwargs, ok):
    pytest.importorskip("dbos")
    from archi.ops.workflow import expected_escalation_id

    io = FakeEscalations(**kwargs)
    verify = functools.partial(_live.ops_verifier, io=io)
    scheduler = _scheduler()
    call = lambda: slurm.drain_node(payload=DRAIN_001, context=CONTEXT, runner=scheduler, verify=verify)  # noqa: E731
    if ok:
        assert call()["status"] == "succeeded"
        assert scheduler.writes()
    else:
        with pytest.raises(ActRefused):
            call()
        assert scheduler.calls == []
    assert io.asked == [expected_escalation_id("wf-1")], "the escalation is the one named by this run's workflow id"


def test_the_ops_runtime_mediator_step_and_the_tool_both_refuse(monkeypatch):
    """Through `run_mediation`: a missing escalation stops at the step; an approval the tool cannot confirm stops at the tool."""
    pytest.importorskip("dbos")
    from archi.ops import workflow as wf

    class IO(FakeEscalations):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.attempts = {}

        def mediation(self, key):
            return self.attempts.get(key)

        def begin_mediation(self, key, proposal_id):
            self.attempts[key] = {"status": "attempted", "result": None}

        def finish_mediation(self, key, status, result):
            self.attempts[key] = {"status": status, "result": result}

    now = datetime(2026, 10, 25, 12, 2, tzinfo=timezone.utc)
    esc = {"escalation_id": wf.expected_escalation_id("wf-1"), "proposal_id": "prop-1",
           "opened_at": datetime(2026, 10, 25, 12, tzinfo=timezone.utc).isoformat(),
           "timeout_at": (datetime(2026, 10, 25, 12, tzinfo=timezone.utc) + timedelta(minutes=10)).isoformat()}

    def mediate(io, esc_arg, verify):
        scheduler = _scheduler()
        tool = functools.partial(slurm.drain_node, runner=scheduler, verify=verify)
        out = wf.run_mediation(
            io, mediator_entry=lambda: "archi.tools.slurm:drain_node", action_class="drain_node",
            payload=DRAIN_001, proposal_id="prop-1", idempotency_key="run-77", esc=esc_arg, workflow_id="wf-1",
            dsn="postgresql://unused", halt=lambda io: None, now=lambda: now, resolve=lambda entry: tool,
            refused=lambda *a: None)
        return out, scheduler

    # approved by a person, in time, and the tool confirms it: the node is drained, once
    io = IO()
    out, scheduler = mediate(io, esc, functools.partial(_live.ops_verifier, io=io))
    assert out["status"] == "succeeded" and len(scheduler.writes()) == 1

    # no escalation at all: refused by the step before the tool is called
    io = IO()
    out, scheduler = mediate(io, None, lambda ctx: pytest.fail("the tool must not be reached"))
    assert out == {"refused": ["error", "no escalation was opened for this proposal"]} and scheduler.calls == []

    # the step let it through but the tool cannot confirm the approval: recorded as a failed call, nothing run
    io = IO()
    out, scheduler = mediate(io, esc, lambda ctx: None)
    assert out["status"] == "failed" and "ActRefused" in out["result"] and scheduler.calls == []


# --- payloads ------------------------------------------------------------------------------


@pytest.mark.parametrize("name, payload", [
    ("drain_node", {"node": "node042"}),                                   # reason missing
    ("drain_node", {"node": "node042", "reason": "ok"}),                   # reason too short
    ("drain_node", {"node": "node042", "reason": "x" * 201}),
    ("drain_node", {"node": "node042", "reason": "bad\nreason"}),          # control character
    ("drain_node", {"node": "node042", "reason": "scratch full\n"}),       # a trailing newline too
    ("drain_node", {"node": "node042", "reason": "scratch full", "force": True}),   # unknown field
    ("drain_node", {"node": "-w", "reason": "scratch full"}),              # looks like an option
    ("drain_node", {"node": "n1,n2", "reason": "scratch full"}),           # one node only
    ("resume_node", {"node": "node[01-09]"}),
    ("resume_node", {}),
    ("requeue_job", {"job_id": "12; scancel 1"}),
    ("requeue_job", {"job_id": 1001}),                                      # a string, as the schema says
    ("requeue_job", {"job_id": "1001", "hold": True}),
])
def test_a_payload_outside_the_schema_is_refused_and_runs_nothing(name, payload):
    scheduler = _scheduler()
    with pytest.raises(ToolError):
        getattr(slurm, name)(payload=payload, context=CONTEXT, runner=scheduler, verify=approved)
    assert scheduler.calls == []


def test_a_payload_that_is_not_an_object_is_refused():
    with pytest.raises(ToolError, match="object"):
        slurm.resume_node(payload=["node1"], context=CONTEXT, runner=_scheduler(), verify=approved)


# --- the acts, idempotent ----------------------------------------------------------------------


def test_drain_sets_the_state_with_the_key_in_the_reason_and_a_repeat_changes_nothing():
    scheduler = FakeScheduler(nodes={"node001": ["mixed", ""]})
    first = slurm.drain_node(payload={"node": "node001", "reason": "scratch full"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert first["status"] == "succeeded" and first["boundary"] == "external_live"
    assert scheduler.writes() == [["scontrol", "update", "NodeName=node001", "State=DRAIN", "Reason=archi[run-77] scratch full"]]
    again = slurm.drain_node(payload={"node": "node001", "reason": "scratch full"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert again["status"] == "succeeded" and "already drained by this call" in again["result"]
    assert len(scheduler.writes()) == 1, "the repeat read the node and wrote nothing"


def test_drain_leaves_a_node_someone_else_drained_alone():
    scheduler = FakeScheduler(nodes={"node001": ["drained", "bad DIMM [bob@2026-10-12]"]})
    out = slurm.drain_node(payload={"node": "node001", "reason": "scratch full"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert out["status"] == "succeeded" and "bad DIMM" in out["result"] and scheduler.writes() == []


def test_drain_of_an_unknown_node_fails_without_a_write():
    scheduler = FakeScheduler()
    out = slurm.drain_node(payload={"node": "nope", "reason": "scratch full"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert out["status"] == "failed" and "not known" in out["result"] and scheduler.writes() == []


def test_resume_returns_a_drained_node_to_service_once():
    scheduler = FakeScheduler(nodes={"node042": ["drained", "scratch full"], "node001": ["mixed", ""]})
    out = slurm.resume_node(payload={"node": "node042"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert out["status"] == "succeeded"
    assert scheduler.writes() == [["scontrol", "update", "NodeName=node042", "State=RESUME"]]
    again = slurm.resume_node(payload={"node": "node042"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert "already in service" in again["result"] and len(scheduler.writes()) == 1
    assert "already in service" in slurm.resume_node(
        payload={"node": "node001"}, context=CONTEXT, runner=scheduler, verify=approved)["result"]
    assert len(scheduler.writes()) == 1


def test_resume_also_returns_a_down_node():
    scheduler = FakeScheduler(nodes={"gpu02": ["down*", "Not responding"]})
    assert slurm.resume_node(payload={"node": "gpu02"}, context=CONTEXT, runner=scheduler, verify=approved)["status"] == "succeeded"
    assert len(scheduler.writes()) == 1


def test_requeue_restarts_a_running_job_once_and_a_repeat_finds_it_waiting():
    scheduler = FakeScheduler(jobs={"1001": "RUNNING"})
    out = slurm.requeue_job(payload={"job_id": "1001"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert out["status"] == "succeeded" and scheduler.writes() == [["scontrol", "requeue", "1001"]]
    again = slurm.requeue_job(payload={"job_id": "1001"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert again["status"] == "succeeded" and "already waiting" in again["result"] and len(scheduler.writes()) == 1


@pytest.mark.parametrize("jobs, fragment", [
    ({}, "not in the queue"),
    ({"1001": "COMPLETED"}, "is COMPLETED, not running"),
    ({"1001": "FAILED"}, "is FAILED, not running"),
])
def test_requeue_of_a_job_that_is_not_running_fails_without_a_write(jobs, fragment):
    scheduler = FakeScheduler(jobs=jobs)
    out = slurm.requeue_job(payload={"job_id": "1001"}, context=CONTEXT, runner=scheduler, verify=approved)
    assert out["status"] == "failed" and fragment in out["result"] and scheduler.writes() == []


def test_a_failing_scontrol_is_a_failed_outcome_not_an_exception():
    def runner(argv, *, timeout):
        if argv[0] == "sinfo":
            return RunResult(0, "node001|cpu|mixed|64|256000|none\n")
        return RunResult(1, "", "scontrol: error: Invalid user id")

    out = slurm.drain_node(payload={"node": "node001", "reason": "scratch full"}, context=CONTEXT, runner=runner, verify=approved)
    assert out["status"] == "failed" and "Invalid user id" in out["result"]


# --- what the pack declares matches the code --------------------------------------------------------


def test_every_tool_the_module_declares_is_a_live_tool_of_the_pack_and_idempotent():
    pack = yaml.safe_load((PACK / "pack.yaml").read_text())
    declared = {f"archi.tools.slurm:{name}" for name in slurm.TOOLS}
    assert set(pack["live_tools"]) == declared and set(pack["idempotent_tools"]) == declared
    for name, meta in slurm.TOOLS.items():
        assert callable(getattr(slurm, name))
        assert meta["idempotent"] is True and meta["boundary"] == "external_live"
    assert {n for n, m in slurm.TOOLS.items() if m["kind"] == "act"} == set(slurm.PAYLOAD_SCHEMAS)


def test_the_action_classes_are_the_act_tools_at_l2_with_the_payload_schemas_the_tools_check():
    classes = yaml.safe_load((PACK / "action_classes.yaml").read_text())["action_classes"]
    assert set(classes) == set(slurm.PAYLOAD_SCHEMAS)
    for name, cls in classes.items():
        assert cls["mediator"] == f"archi.tools.slurm:{name}"
        assert cls["max_level"] == "L2", "3.0 has nothing above L2, and an act needs an approval"
        assert cls["payload_schema"] == slurm.PAYLOAD_SCHEMAS[name]
        assert "external_live" in cls["blast_radius"] or "Slurm" in cls["blast_radius"]
    assert classes["requeue_job"]["reversible"] is False
    assert classes["drain_node"]["reversible"] is True and classes["resume_node"]["reversible"] is True


def test_the_act_tools_are_mediators_only_never_agent_tools():
    """The module's docstring wires only the two reads as agent tools."""
    wired = [line.split(":")[0].strip() for line in slurm.__doc__.splitlines() if line.strip().endswith(":") and line.startswith("      archi_")]
    assert wired == ["archi_slurm_squeue", "archi_slurm_sinfo"]
    for name in ("drain_node", "resume_node", "requeue_job"):
        assert name not in " ".join(wired)
