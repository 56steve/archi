"""Slurm live tools: read the queue and the nodes, drain or resume a node, requeue a job.

Every tool here has the boundary ``external_live``: what it returns is a live
reading from the scheduler, not graph evidence. Wiring for an instance's
``agent_tools`` (the two read tools; the act tools are not agent tools)::

    agent_tools:
      archi_slurm_squeue:
        module: archi.tools.slurm
        function: archi_slurm_squeue
        boundary: external_live
        optional: true
      archi_slurm_sinfo:
        module: archi.tools.slurm
        function: archi_slurm_sinfo
        boundary: external_live
        optional: true

**Read tools** (``archi_slurm_squeue``, ``archi_slurm_sinfo``) build one
``squeue`` or ``sinfo`` command from validated arguments and return bounded rows.

**Act tools** (``drain_node``, ``resume_node``, ``requeue_job``) are the mediators
of the action classes in this pack's ``action_classes.yaml``, each at most L2:

    drain_node(payload={"node", "reason"}, context=...)
    resume_node(payload={"node"}, context=...)
    requeue_job(payload={"job_id"}, context=...)

They are not agent tools. The ops runtime calls them only after a person
approved the escalation, and each checks that again before it builds a command
(:func:`archi.tools._live.require_approval`): a call without an approved
decision raises ``ActRefused`` and runs nothing. Each is idempotent. The
runtime passes the run's ``idempotency_key``; the tool reads the node or job
first and does nothing when it is already in the requested state, and a drain
writes the key into the node's Slurm reason so a repeat finds its own mark.

The commands (``squeue``, ``sinfo``, ``scontrol``) run through an injected
``runner``; see :mod:`archi.tools._live`. Credentials: none in archi. The
runner runs as the account the instance gives it (a Slurm operator account on
the controller or a login node).
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from archi.tools import _live
from archi.tools._live import (
    ActRefused,
    Runner,
    ToolError,
    Verifier,
    bounded,
    job_id_arg,
    name_arg,
    nodelist_arg,
    outcome,
    require_approval,
    run_read,
    split_fields,
    subprocess_runner,
    validate_payload,
)

__all__ = [
    "ActRefused", "ToolError", "TOOLS", "PAYLOAD_SCHEMAS",
    "archi_slurm_squeue", "archi_slurm_sinfo", "drain_node", "resume_node", "requeue_job",
]

SQUEUE_FIELDS = ("job_id", "partition", "name", "user", "state", "elapsed", "nodes", "reason_or_nodelist")
SQUEUE_FORMAT = "%i|%P|%j|%u|%T|%M|%D|%R"
SINFO_FIELDS = ("node", "partition", "state", "cpus", "memory_mb", "reason")
SINFO_FORMAT = "%N|%P|%T|%c|%m|%E"

#: states `scontrol` reports for a node that is out of service by an operator or by Slurm
_OUT_OF_SERVICE = ("drain", "drng", "down", "fail", "failg", "maint")

PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {
    "drain_node": {
        "type": "object",
        "required": ["node", "reason"],
        "additionalProperties": False,
        "properties": {
            "node": {"type": "string", "pattern": _live.NAME_PATTERN},
            "reason": {"type": "string", "minLength": 3, "maxLength": 200, "pattern": r"^[^\x00-\x1f\x7f]+$"},
        },
    },
    "resume_node": {
        "type": "object",
        "required": ["node"],
        "additionalProperties": False,
        "properties": {"node": {"type": "string", "pattern": _live.NAME_PATTERN}},
    },
    "requeue_job": {
        "type": "object",
        "required": ["job_id"],
        "additionalProperties": False,
        "properties": {"job_id": {"type": "string", "pattern": _live.JOB_ID_PATTERN}},
    },
}

#: What the pack ships, in one place: the tests hold this equal to pack.yaml and action_classes.yaml.
TOOLS: dict[str, dict[str, Any]] = {
    "archi_slurm_squeue": {"kind": "read", "idempotent": True, "boundary": _live.BOUNDARY},
    "archi_slurm_sinfo": {"kind": "read", "idempotent": True, "boundary": _live.BOUNDARY},
    "drain_node": {"kind": "act", "idempotent": True, "boundary": _live.BOUNDARY, "max_level": "L2"},
    "resume_node": {"kind": "act", "idempotent": True, "boundary": _live.BOUNDARY, "max_level": "L2"},
    "requeue_job": {"kind": "act", "idempotent": True, "boundary": _live.BOUNDARY, "max_level": "L2"},
}


# --- read tools -----------------------------------------------------------------


def _squeue_rows(text: str) -> list[dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) > len(SQUEUE_FIELDS):
            # a job name may hold the separator: the name is the field after the third
            extra = len(parts) - len(SQUEUE_FIELDS)
            parts = parts[:2] + ["|".join(parts[2:3 + extra])] + parts[3 + extra:]
        row = dict(zip(SQUEUE_FIELDS, (p.strip() for p in parts)))
        rows.append(row)
    return rows


def archi_slurm_squeue(
    user: Optional[str] = None,
    partition: Optional[str] = None,
    state: Optional[str] = None,
    job_id: Optional[str] = None,
    max_results: int = 50,
    runner: Optional[Runner] = None,
) -> dict[str, Any]:
    """Live ``squeue``: jobs in the queue, optionally for one user, partition, state or job id."""
    limit = bounded(max_results)
    argv = ["squeue", "--noheader", f"--format={SQUEUE_FORMAT}"]
    try:
        if user:
            argv += ["--user", name_arg(user, "user")]
        if partition:
            argv += ["--partition", name_arg(partition, "partition")]
        if state:
            argv += ["--states", name_arg(state.upper(), "state")]
        if job_id:
            argv += ["--jobs", job_id_arg(job_id)]
    except ToolError as exc:
        return _live.error_payload(argv, str(exc))
    return run_read(argv, runner, _squeue_rows, limit)


def _sinfo_rows(text: str) -> list[dict[str, Any]]:
    return [split_fields(line, SINFO_FIELDS) for line in text.splitlines() if line.strip()]


def archi_slurm_sinfo(
    partition: Optional[str] = None,
    node: Optional[str] = None,
    state: Optional[str] = None,
    max_results: int = 50,
    runner: Optional[Runner] = None,
) -> dict[str, Any]:
    """Live ``sinfo``: nodes with their state and, for a drained node, the reason."""
    limit = bounded(max_results)
    argv = ["sinfo", "--noheader", "--Node", f"--format={SINFO_FORMAT}"]
    try:
        if partition:
            argv += ["--partition", name_arg(partition, "partition")]
        if node:
            argv += ["--nodes", nodelist_arg(node)]
        if state:
            argv += ["--states", name_arg(state.lower(), "state")]
    except ToolError as exc:
        return _live.error_payload(argv, str(exc))
    return run_read(argv, runner, _sinfo_rows, limit)


# --- act tools --------------------------------------------------------------------


def _node_state(node: str, runner: Runner) -> Optional[dict[str, str]]:
    """The node as ``sinfo`` shows it now, or None when Slurm does not know it."""
    result = runner(["sinfo", "--noheader", "--Node", f"--format={SINFO_FORMAT}", "--nodes", node],
                    timeout=_live.DEFAULT_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(f"sinfo exited {result.returncode}: {result.stderr.strip()[:200]}")
    rows = _sinfo_rows(result.stdout)
    return rows[0] if rows else None


def _out_of_service(state: str) -> bool:
    return any(word in state.lower() for word in _OUT_OF_SERVICE)


def _is_drained(state: str) -> bool:
    return "drain" in state.lower() or "drng" in state.lower()


def _prepare(payload: Any, context: Optional[Mapping[str, Any]], verify: Optional[Verifier], tool: str) -> dict[str, Any]:
    require_approval(context, verify)  # first: a refused call validates and runs nothing
    data = validate_payload(payload, PAYLOAD_SCHEMAS[tool])
    # `$` in a schema pattern also matches before a final newline; a reason is one line, checked here too
    if any(ord(ch) < 32 or ch == "\x7f" for ch in data.get("reason", "")):
        raise ToolError("reason may not hold control characters")
    return data


def _scontrol(argv: list[str], runner: Runner) -> Optional[str]:
    """Run one `scontrol`; the error text on failure, None on success."""
    result = runner(argv, timeout=_live.DEFAULT_TIMEOUT)
    if result.returncode != 0:
        return f"scontrol exited {result.returncode}: {result.stderr.strip()[:200]}"
    return None


def drain_node(
    *,
    payload: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
    runner: Optional[Runner] = None,
    verify: Optional[Verifier] = None,
) -> dict[str, Any]:
    """Drain one node: running jobs finish, no new job starts. Reversible with ``resume_node``."""
    data = _prepare(payload, context, verify, "drain_node")
    run = runner or subprocess_runner
    key = context["idempotency_key"]
    node = data["node"]
    state = _node_state(node, run)
    if state is None:
        return outcome("failed", f"node {node} is not known to Slurm; nothing changed")
    mark = f"archi[{key}]"
    if _is_drained(state["state"]):
        if mark in state["reason"]:
            return outcome("succeeded", f"node {node} is already drained by this call ({state['state']})")
        return outcome(
            "succeeded",
            f"node {node} is already {state['state']} with another reason ({state['reason'] or 'none'}); left as is",
        )
    error = _scontrol(
        ["scontrol", "update", f"NodeName={node}", "State=DRAIN", f"Reason={mark} {data['reason']}"], run
    )
    if error:
        return outcome("failed", error)
    return outcome("succeeded", f"node {node} set to DRAIN ({mark})")


def resume_node(
    *,
    payload: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
    runner: Optional[Runner] = None,
    verify: Optional[Verifier] = None,
) -> dict[str, Any]:
    """Return a drained or down node to service."""
    data = _prepare(payload, context, verify, "resume_node")
    run = runner or subprocess_runner
    node = data["node"]
    state = _node_state(node, run)
    if state is None:
        return outcome("failed", f"node {node} is not known to Slurm; nothing changed")
    if not _out_of_service(state["state"]):
        return outcome("succeeded", f"node {node} is already in service ({state['state']})")
    error = _scontrol(["scontrol", "update", f"NodeName={node}", "State=RESUME"], run)
    if error:
        return outcome("failed", error)
    return outcome("succeeded", f"node {node} set to RESUME (was {state['state']})")


def requeue_job(
    *,
    payload: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
    runner: Optional[Runner] = None,
    verify: Optional[Verifier] = None,
) -> dict[str, Any]:
    """Requeue one running job so it starts again from the queue."""
    data = _prepare(payload, context, verify, "requeue_job")
    run = runner or subprocess_runner
    job_id = data["job_id"]
    result = run(["squeue", "--noheader", f"--format={SQUEUE_FORMAT}", "--jobs", job_id], timeout=_live.DEFAULT_TIMEOUT)
    if result.returncode != 0:
        # squeue exits non-zero for a job id it no longer knows
        return outcome("failed", f"job {job_id} is not in the queue ({result.stderr.strip()[:200] or 'unknown'})")
    rows = _squeue_rows(result.stdout)
    if not rows:
        return outcome("failed", f"job {job_id} is not in the queue; nothing changed")
    job_state = rows[0]["state"].upper()
    if job_state in ("PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED"):
        return outcome("succeeded", f"job {job_id} is already waiting in the queue ({job_state})")
    if job_state != "RUNNING":
        return outcome("failed", f"job {job_id} is {job_state}, not running; nothing changed")
    error = _scontrol(["scontrol", "requeue", job_id], run)
    if error:
        return outcome("failed", error)
    return outcome("succeeded", f"job {job_id} requeued")
