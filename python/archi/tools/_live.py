"""Shared parts of the scheduler live tools (``archi.tools.slurm``, ``archi.tools.htcondor``).

Two rules hold for every tool built on this module.

**Commands go through a runner.** A tool never calls ``subprocess`` itself. It
builds an argument list (never a shell string), hands it to a ``Runner`` and
reads the result. The default runner runs the command with ``subprocess.run``
(no shell); tests inject a fake and never start a process. A caller can also
inject a runner that goes over ssh or to a slurmrestd client.

**Act tools need an approved escalation.** An act tool changes a scheduler. It
is a mediator of an action class at L2 (ADR 0002 sections 7.2 and 7.3): the ops
runtime calls it as ``tool(payload=..., context=...)`` only after a person
approved the escalation, and the tool checks that again on its own before it
builds a command (:func:`require_approval`). A call with no decision, a
rejected, expired or foreign one, or no idempotency key is refused with
:class:`ActRefused` and the runner is never called. In 3.0 there is no level
above L2, so there is no path around this check (ADR 0002 D13).

Every payload a tool returns carries ``boundary: external_live`` and
``observed_at``: the readings are not generation-pinned graph evidence.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

BOUNDARY = "external_live"
DEFAULT_TIMEOUT = 30.0
MAX_ROWS = 200

#: A node, partition, user or account name: letters, digits and the few marks
#: Slurm and HTCondor names use. Never a leading dash, so it cannot be read as an option.
NAME_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.:@-]{0,127}$"
#: A Slurm node list such as ``gpu[01-04],cpu7``.
NODELIST_PATTERN = r"^[A-Za-z0-9_][A-Za-z0-9_.,\[\]-]{0,255}$"
#: A Slurm job id: ``123``, an array element ``123_4`` or a pack member ``123+1``.
JOB_ID_PATTERN = r"^[0-9]{1,12}([_+][0-9]{1,9})?$"
_NAME = re.compile(NAME_PATTERN)
_NODELIST = re.compile(NODELIST_PATTERN)
_JOB_ID = re.compile(JOB_ID_PATTERN)


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def __call__(self, argv: Sequence[str], *, timeout: float) -> RunResult: ...


def subprocess_runner(argv: Sequence[str], *, timeout: float) -> RunResult:
    """Run one command without a shell. Only a deployed tool reaches this; tests inject a fake."""
    done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, check=False)
    return RunResult(done.returncode, done.stdout, done.stderr)


class ToolError(ValueError):
    """The call's own arguments are wrong. Nothing was run."""


class ActRefused(RuntimeError):
    """An act tool refused to run: no approved decision for this call. Nothing was run."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def name_arg(value: Any, what: str) -> str:
    text = str(value) if value is not None else ""
    if not _NAME.fullmatch(text):
        raise ToolError(f"{what} {text!r} is not a plain name (letters, digits and _ . : @ -)")
    return text


def nodelist_arg(value: Any, what: str = "node") -> str:
    text = str(value) if value is not None else ""
    if not _NODELIST.fullmatch(text):
        raise ToolError(f"{what} {text!r} is not a node name or node list")
    return text


def job_id_arg(value: Any) -> str:
    text = str(value) if value is not None else ""
    if not _JOB_ID.fullmatch(text):
        raise ToolError(f"job id {text!r} is not a Slurm job id (123, 123_4 or 123+1)")
    return text


def bounded(limit: Any, default: int = 50) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_ROWS))


def read_payload(command: Sequence[str], *, rows: list[dict[str, Any]], limit: int, **extra: Any) -> dict[str, Any]:
    """The payload every read tool returns."""
    return {
        "boundary": BOUNDARY,
        "observed_at": now(),
        "command": list(command),
        "count": len(rows),
        "truncated": len(rows) > limit,
        "rows": rows[:limit],
        **extra,
    }


def error_payload(command: Sequence[str], message: str, **extra: Any) -> dict[str, Any]:
    return {"boundary": BOUNDARY, "observed_at": now(), "command": list(command), "error": message, **extra}


def run_read(
    command: Sequence[str],
    runner: Optional[Runner],
    parse: Callable[[str], list[dict[str, Any]]],
    limit: int,
    timeout: float = DEFAULT_TIMEOUT,
    **extra: Any,
) -> dict[str, Any]:
    """Run a read-only command and shape its output; a failure comes back as an ``error`` payload."""
    try:
        result = (runner or subprocess_runner)(command, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return error_payload(command, f"{type(exc).__name__}: {exc}", **extra)
    if result.returncode != 0:
        return error_payload(
            command, f"{command[0]} exited {result.returncode}: {result.stderr.strip()[:300]}", **extra
        )
    return read_payload(command, rows=parse(result.stdout), limit=limit, **extra)


def split_fields(line: str, names: Sequence[str], sep: str = "|") -> dict[str, str]:
    """One delimited line as a dict; the last field keeps any extra separators (free text)."""
    parts = line.rstrip("\n").split(sep, len(names) - 1)
    parts += [""] * (len(names) - len(parts))
    return {name: part.strip() for name, part in zip(names, parts)}


# --- the approval check -----------------------------------------------------


@dataclass(frozen=True)
class Approval:
    escalation_id: str
    proposal_id: str
    decided_by: str


#: ``verify(context) -> Approval | None``: the approved decision bound to this call, else None.
Verifier = Callable[[Mapping[str, Any]], Optional[Approval]]


def ops_verifier(context: Mapping[str, Any], io: Optional[Any] = None) -> Optional[Approval]:
    """The approved decision for the run in ``context``, read from okg.

    The runtime names the run's escalation from its workflow id, so the check
    needs nothing from the caller beyond what the runtime already passes
    (``dsn``, ``workflow_id``, ``proposal_id``). ``io`` is anything with
    ``decision_for(escalation_id)`` (a test passes a fake; a deployed tool
    builds an ``OpsIO`` on the run's database). Imported here, not at the top,
    so reading these tools does not load the ops runtime.
    """
    from archi.ops.escalation import APPROVED
    from archi.ops.workflow import expected_escalation_id

    if io is None:
        from archi.ops.okg_io import OpsIO
        from archi.ops.runtime_config import load_runtime

        io = OpsIO(str(context["dsn"]), load_runtime().deployment)
    escalation_id = expected_escalation_id(str(context["workflow_id"]))
    decision = io.decision_for(escalation_id)
    if decision.status != APPROVED or not decision.decided_by:
        return None
    if decision.proposal_id != context.get("proposal_id") or decision.escalation_id != escalation_id:
        return None
    return Approval(escalation_id, str(decision.proposal_id), str(decision.decided_by))


REQUIRED_CONTEXT = ("idempotency_key", "proposal_id", "workflow_id")


def require_approval(context: Optional[Mapping[str, Any]], verify: Optional[Verifier] = None) -> Approval:
    """Return the approved decision for this call or raise :class:`ActRefused`.

    Refuses when the context is missing a key the runtime always sets, when no
    approved decision is bound to this proposal, and when the check itself
    fails (it fails closed: a database that cannot be read is not an approval).
    """
    if not isinstance(context, Mapping):
        raise ActRefused("no call context: an act tool runs only as a mediator, after an approved escalation")
    missing = [key for key in REQUIRED_CONTEXT if not context.get(key)]
    if missing:
        raise ActRefused(f"call context lacks {', '.join(missing)}: not called by the ops runtime")
    try:
        approval = (verify or ops_verifier)(context)
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is a refusal
        raise ActRefused(f"could not verify an approved escalation ({type(exc).__name__}: {exc})") from exc
    if approval is None:
        raise ActRefused(
            f"no approved escalation for proposal {context['proposal_id']}: act tools run only after a person approves"
        )
    if approval.proposal_id != context["proposal_id"]:
        raise ActRefused(f"the approval is for proposal {approval.proposal_id}, not {context['proposal_id']}")
    return approval


def validate_payload(payload: Any, schema: Mapping[str, Any]) -> dict[str, Any]:
    """Check an act payload against its schema; unknown fields are refused."""
    from jsonschema import Draft202012Validator

    if not isinstance(payload, Mapping):
        raise ToolError("payload must be an object")
    errors = sorted(Draft202012Validator(schema).iter_errors(dict(payload)), key=lambda e: list(e.path))
    if errors:
        raise ToolError("; ".join(f"{'.'.join(map(str, e.path)) or 'payload'}: {e.message}" for e in errors))
    return dict(payload)


def outcome(status: str, result: str, **extra: Any) -> dict[str, Any]:
    """What a mediator returns: ``status`` and ``result`` are what the runtime records."""
    return {"status": status, "result": result, "boundary": BOUNDARY, "observed_at": now(), **extra}
