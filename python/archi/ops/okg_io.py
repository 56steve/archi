"""The database, okg and DBOS calls the ops runtime and `archi ops` make.

One class, `OpsIO`, so the worker and the CLI read the same things the same
way and tests can swap it for a fake. It imports okg's substrate (never
`okg.deployment`; that import lives only in `archi.ops.workflow`), `psycopg`
and `dbos`, each when first used.

Escalations are read from `okg.escalation_live` (current version:
`valid_to_gen IS NULL`). Operators answer through okg's own
`respond_to_escalation`; the `okg runtime escalation respond` command is
broken in okg 0.2.0 (ImportError in cli/runtime.py:1154).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

from archi.ops.control import LIVE_RUN_STATES, RunInfo
from archi.ops.escalation import OPEN_STATES, SUBJECT_PREFIX, Decision, decision_from_row
from archi.ops.record import RecordError, _okg
from archi.ops.runtime_config import RUN_WORKFLOW, SCANNER

SIGNALS_DDL = """
CREATE SCHEMA IF NOT EXISTS archi_ops;
CREATE TABLE IF NOT EXISTS archi_ops.signals (
    automation text NOT NULL,
    signal_id  text NOT NULL,
    entity     text NOT NULL,
    title      text NOT NULL,
    source     text NOT NULL DEFAULT 'table',
    summary    text,
    status     text NOT NULL DEFAULT 'new',
    attempt    integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (automation, signal_id)
);
-- schedules `archi ops halt` paused, so `resume` leaves alone any an operator paused earlier
CREATE TABLE IF NOT EXISTS archi_ops.halts (
    schedule  text PRIMARY KEY,
    paused_at timestamptz NOT NULL DEFAULT now(),
    paused_by text NOT NULL
);
"""


def _val(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)


@dataclass
class OpsIO:
    dsn: str
    deployment: str
    okg: Optional[str] = None

    # --- connections ---------------------------------------------------------

    def connect(self):
        import psycopg

        return psycopg.connect(self.dsn, autocommit=True)

    def _client(self):
        """A DBOSClient on the deployment's database (as okg's admin code builds it)."""
        from dbos import DBOSClient

        return DBOSClient(database_url=self.dsn, system_database_url=self.dsn, application_database_url=self.dsn)

    def _okg(self, *args: str) -> subprocess.CompletedProcess:
        cmd = [self.okg or _okg(), *args, "--dsn", self.dsn]
        return subprocess.run(cmd, capture_output=True, text=True)

    # --- the graph -----------------------------------------------------------

    def pin_generation(self) -> str:
        """The latest published generation (the checkpoint id okg's readers take)."""
        from okg.substrate.generation_resolver import resolve_okg_generation

        with self.connect() as conn:
            resolved = resolve_okg_generation(conn, deployment_name=self.deployment)
        if resolved is None:
            raise RecordError(f"deployment {self.deployment!r} has no published generation")
        return resolved.generation_id

    def read_nodes(self, subtype: str, generation: str, where: Optional[Mapping[str, str]] = None) -> list[dict]:
        """Every node of `subtype` at `generation` matching `where` ({attr: value})."""
        args = ["filter", "--deployment", self.deployment, "--type", subtype, "--generation-id", str(generation),
                "--all", "--jsonl"]
        for key, value in (where or {}).items():
            args += ["--where", f"attrs.{key}={value}"]
        result = self._okg(*args)
        if result.returncode != 0:
            raise RecordError(f"okg filter --type {subtype} failed: {(result.stderr or result.stdout)[-2000:]}")
        return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]

    def doctor_sources(self) -> list[dict]:
        """okg's per-source rows (`okg doctor --check sources`), with watermarks."""
        result = self._okg("doctor", "--check", "sources", "--deployment", self.deployment, "--json")
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise RecordError(f"okg doctor did not answer: {(result.stderr or result.stdout)[-1000:]}") from None
        rows = data.get("sources") if isinstance(data, Mapping) else None
        if rows is None:
            raise RecordError("okg doctor --check sources returned no `sources` list")
        return list(rows)

    # --- escalations ---------------------------------------------------------

    def escalation(self, escalation_id: str) -> Optional[tuple[str, dict]]:
        """(subject node id, attrs) of the current version of an escalation, or None."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT subject_node_id, attrs FROM okg.escalation_live "
                "WHERE escalation_id = %s AND valid_to_gen IS NULL", (escalation_id,)).fetchone()
        return (row[0], dict(row[1])) if row else None

    def decision_for(self, escalation_id: str) -> Decision:
        found = self.escalation(escalation_id)
        return decision_from_row(escalation_id, found[0] if found else None, found[1] if found else None)

    def escalations(self, *, open_only: bool = True, workflow_ids: Optional[Sequence[str]] = None) -> list[dict]:
        """Escalations about ops proposals, newest first, with the fields an operator needs."""
        sql = ("SELECT escalation_id, subject_node_id, attrs FROM okg.escalation_live "
               "WHERE valid_to_gen IS NULL AND subject_node_id LIKE %s")
        params: list[Any] = [SUBJECT_PREFIX + "%"]
        if open_only:
            sql += " AND attrs->>'status' = ANY(%s)"
            params.append(list(OPEN_STATES))
        if workflow_ids is not None:
            sql += " AND attrs->>'requested_by_workflow' = ANY(%s)"
            params.append(list(workflow_ids))
        sql += " ORDER BY attrs->>'timeout_at'"
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"escalation_id": r[0], "proposal_id": r[1][len(SUBJECT_PREFIX):], **{
            k: r[2].get(k) for k in ("status", "reason", "timeout_at", "decided_by", "decided_at",
                                     "requested_by_workflow")}} for r in rows]

    def respond(self, escalation_id: str, verdict: str, *, note: Optional[str], decided_by: str) -> Any:
        """Answer an escalation (approve | reject). Raises okg's EscalationStateError if it is closed."""
        from okg.substrate.db.connection import connect
        from okg.substrate.runtime.escalation_response import respond_to_escalation

        decision = {"verdict": verdict, **({"note": note} if note else {})}
        with connect(self.dsn) as conn:
            return respond_to_escalation(conn, deployment=self.deployment, escalation_id=escalation_id,
                                         decision=decision, decided_by=decided_by, send_wake=True)

    # --- schedules and runs --------------------------------------------------

    def schedules(self) -> dict[str, bool]:
        """The ops scanner schedule and whether it is paused."""
        out = {}
        for entry in self._client().list_schedules():
            name = _val(entry, "schedule_name")
            if name == SCANNER:
                out[name] = str(_val(entry, "status")).upper() == "PAUSED"
        return out

    def pause(self, name: str) -> None:
        client = self._client()
        client.pause_schedule(name)
        if str(_val(client.get_schedule(name), "status")).upper() != "PAUSED":
            raise RecordError(f"schedule {name!r} did not persist PAUSED")

    def resume(self, name: str) -> None:
        client = self._client()
        client.resume_schedule(name)
        if str(_val(client.get_schedule(name), "status")).upper() != "ACTIVE":
            raise RecordError(f"schedule {name!r} did not persist ACTIVE")

    def runs(self, *, live_only: bool = True) -> list[RunInfo]:
        statuses = sorted(LIVE_RUN_STATES) if live_only else None
        out = []
        for wf in self._client().list_workflows(name=RUN_WORKFLOW, status=statuses, load_input=True):
            wid, status = _val(wf, "workflow_id"), str(_val(wf, "status"))
            args = (_val(wf, "input") or {}).get("args") or []
            out.append(RunInfo(wid, args[0] if args else wid, status))
        return out

    def run_input(self, workflow_id: str) -> Mapping[str, Any]:
        """The `signal` argument a run was started with."""
        for wf in self._client().list_workflows(workflow_ids=[workflow_id], load_input=True):
            args = (_val(wf, "input") or {}).get("args") or []
            if len(args) > 1:
                return args[1]
        return {}

    def cancel(self, workflow_id: str) -> None:
        self._client().cancel_workflow(workflow_id)

    def cancel_unconfirmed_admission(self, idempotency_key: str) -> list[str]:
        """Cancel the queued okg admission workflow(s) submitted with `idempotency_key`.

        An `okg admission submit` made while no worker runs leaves an ENQUEUED
        `okg_admission_submit` with no application version, and okg 0.2.0 then refuses to
        start a worker (RuntimeCutoverBlocked) until it is cancelled. Only workflows whose
        input carries this exact key are touched. Returns the cancelled workflow ids.
        """
        client = self._client()
        cancelled = []
        for wf in client.list_workflows(name="okg_admission_submit", status=["ENQUEUED"], load_input=True):
            if idempotency_key in json.dumps(_val(wf, "input"), default=str):
                client.cancel_workflow(_val(wf, "workflow_id"))
                cancelled.append(_val(wf, "workflow_id"))
        return cancelled

    # --- budgets -------------------------------------------------------------

    def day_spend(self, automation: str, today: Optional[datetime] = None) -> tuple[Optional[float], Optional[float]]:
        """(spent, cap) of the automation's budget for the UTC day, None when never used."""
        day = (today or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
        scope = day_scope(automation, day)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT caps.cap_usd, COALESCE(sum(l.usd_consumed), 0) FROM okg.deployment_cost_caps caps "
                "LEFT JOIN okg.deployment_cost_ledger l ON l.scope = caps.scope "
                "WHERE caps.scope = %s GROUP BY caps.cap_usd", (scope,)).fetchone()
        return (float(row[1]), float(row[0])) if row else (None, None)

    # --- signals (the table trigger the scratch automations use) and halt state ---

    def ensure_tables(self) -> None:
        with self.connect() as conn:
            conn.execute(SIGNALS_DDL)

    def record_halt(self, schedules: Sequence[str], by: str) -> None:
        with self.connect() as conn:
            for name in schedules:
                conn.execute("INSERT INTO archi_ops.halts (schedule, paused_by) VALUES (%s, %s) "
                             "ON CONFLICT DO NOTHING", (name, by))

    def paused_by_halt(self) -> list[str]:
        with self.connect() as conn:
            return [r[0] for r in conn.execute("SELECT schedule FROM archi_ops.halts").fetchall()]

    def clear_halt(self, schedule: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM archi_ops.halts WHERE schedule = %s", (schedule,))

    def add_signal(self, automation: str, signal_id: str, entity: str, title: str, *, summary: Optional[str] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO archi_ops.signals (automation, signal_id, entity, title, summary) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING", (automation, signal_id, entity, title, summary))

    def new_signals(self, automation: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT signal_id, entity, title, source, summary, attempt FROM archi_ops.signals "
                "WHERE automation = %s AND status = 'new' ORDER BY created_at, signal_id", (automation,)).fetchall()
        return [dict(zip(("id", "entity", "title", "source", "summary", "attempt"), r)) for r in rows]

    def mark_signal_started(self, automation: str, signal_id: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE archi_ops.signals SET status = 'started' WHERE automation = %s AND signal_id = %s",
                         (automation, signal_id))


def day_scope(automation: str, day: str) -> str:
    """The cost_budget scope of one automation's UTC day."""
    return f"day:{automation}:{day}"
