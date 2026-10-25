"""Helpers for scratch_ops_runtime_run.sh: set the instance up, configure it, add signals, wait on runs,
change the world for a test, look at escalations, markers and watermarks. Scratch use only.

    python scratch_ops_runtime_tools.py <command> ...

Environment: ARCHI_INSTANCE, OKG_DSN, ARCHI_OPS_DEPLOYMENT.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pickle
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import yaml

from archi.ops.okg_io import OpsIO

AUTOMATION = "scratch-triage"
SEED_INCIDENT = "ops_incident:inc-seed-1"
SEED_ENTITY = "T2_US_MIT"
SOURCE = "jira.jira"
OPERATOR = "operator-x"

PROPOSALS = {
    "marker": {
        "summary": "restart the SRM service at the site",
        "evidence_ids": [SEED_INCIDENT],
        "action_class": "write_marker",
        "payload": {"entity": SEED_ENTITY, "note": "restart srm"},
        "confidence": 0.9,
        "rollback": "start it again",
    },
    "slow": {
        "summary": "restart the SRM service at the site (the tool lingers after it writes)",
        "evidence_ids": [SEED_INCIDENT],
        "action_class": "write_marker",
        "payload": {"entity": SEED_ENTITY, "note": "slow:40"},
        "confidence": 0.9,
        "rollback": "start it again",
    },
    "draft": {
        "summary": "reply to the ticket",
        "evidence_ids": [SEED_INCIDENT],
        "action_class": "draft_reply",
        "payload": {"ticket_key": "OPS-12", "body": "We are looking at the SRM timeouts."},
        "confidence": 0.7,
        "rollback": "delete the draft",
    },
}


def env() -> tuple[Path, str, str]:
    return Path(os.environ["ARCHI_INSTANCE"]), os.environ["OKG_DSN"], os.environ["ARCHI_OPS_DEPLOYMENT"]


def io() -> OpsIO:
    _, dsn, deployment = env()
    return OpsIO(dsn, deployment)


def load(path: Path) -> dict:
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}


def cmd_init_instance(a: argparse.Namespace) -> None:
    """What `archi init` does after rendering, through the same functions (archi.cli.init.install_and_publish):
    git-init the instance, write the secrets and archi.yaml, okg install, commit, provision (with the runtime
    bootstrap), role passwords. Only the container start is left out: the lab database already runs."""
    from archi.cli import init as init_mod
    from archi.stack.instance import GITIGNORE, Instance, Ports, generate_secrets, write_env
    from archi.stack.okg import Okg, okg_version

    inst_dir, dsn, deployment = env()
    parsed = urlparse(dsn)
    data_root = inst_dir.parent / "data"
    init_mod.git(inst_dir, "init", "-q", "-b", "main")
    (inst_dir / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    init_mod.commit_all(inst_dir, "baseline: the rendered instance")
    instance = Instance(dir=inst_dir, name=deployment, bundles=["cern-team"], data_root=data_root, runtime="docker",
                        ports=Ports(graph_db=parsed.port, mcp=18765, webui=18080), profile="cern-team",
                        okg_version=okg_version() or "", admin_email=f"admin@{deployment}.archi.invalid")
    secrets = generate_secrets()
    secrets["ARCHI_GRAPH_DB_PASSWORD"] = parsed.password
    secrets["ARCHI_OKG_MCP_PASSWORD"] = ""
    write_env(instance.env_path, secrets)
    instance.save()
    resolved = init_mod.absolute_answers(init_mod.profile_questions(inst_dir / "profiles" / "cern-team"), {},
                                         data_root, Path.cwd())
    init_mod.install_and_publish(instance, secrets, Okg(instance, secrets, echo=print), "cern-team", resolved, echo=print)


def cmd_configure(a: argparse.Namespace) -> None:
    """Merge settings into archi.yaml (the level) and ops.yaml (mode, timeout, stub proposal and cost, gate)."""
    inst, _, deployment = env()
    archi = load(inst / "archi.yaml")
    archi.setdefault("name", deployment)
    if a.level:
        archi.setdefault("automations", {}).setdefault(AUTOMATION, {})["level"] = a.level
    (inst / "archi.yaml").write_text(yaml.safe_dump(archi, sort_keys=False), encoding="utf-8")
    ops = load(inst / "ops.yaml")
    entry = ops.setdefault("automations", {}).setdefault(AUTOMATION, {})
    if a.mode:
        entry["mode"] = a.mode
    if a.timeout is not None:
        entry["timeout_seconds"] = a.timeout
    if a.gate is not None:
        entry["test_gate_seconds"] = a.gate
    if a.proposal or a.cost is not None:
        fixture = (entry.get("proposer") or {}).get("fixture") or {}
        proposal = PROPOSALS[a.proposal] if a.proposal else fixture.get("default")
        cost = a.cost if a.cost is not None else fixture.get("cost", 0.1)
        entry["proposer"] = {"kind": "stub", "fixture": {"default": proposal, "cost": cost}}
    entry.setdefault("outcome", {"kind": "marker"})
    if a.estimate is not None:
        entry["estimate_cost"] = a.estimate
    (inst / "ops.yaml").write_text(yaml.safe_dump(ops, sort_keys=False), encoding="utf-8")


def cmd_signal(a: argparse.Namespace) -> None:
    io().add_signal(AUTOMATION, a.id, SEED_ENTITY, f"SRM timeouts again ({a.id})")
    print(f"{AUTOMATION}~{a.id}~{a.attempt}")


def decode(output):
    """DBOS stores a workflow's return value as a base64 pickle (serialization py_pickle)."""
    if isinstance(output, str):
        try:
            return pickle.loads(base64.b64decode(output, validate=True))
        except Exception:
            try:
                return json.loads(output)
            except ValueError:
                return output
    return output


def run_status(conn, wid: str):
    row = conn.execute("SELECT status, output FROM dbos.workflow_status WHERE workflow_uuid = %s", (wid,)).fetchone()
    return (row[0], decode(row[1])) if row else None


def cmd_wait(a: argparse.Namespace) -> None:
    """Wait until the run is final (SUCCESS, ERROR, CANCELLED) or `--status` is reached."""
    deadline = time.time() + a.timeout
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
        while time.time() < deadline:
            row = run_status(conn, a.wid)
            if row and (row[0] == a.status if a.status else row[0] in ("SUCCESS", "ERROR", "CANCELLED")):
                print(json.dumps({"status": row[0], "output": row[1]}, default=str))
                return
            time.sleep(1)
        row = run_status(conn, a.wid)
    print(json.dumps({"timeout": True, "status": row[0] if row else None}))
    sys.exit(3)


def cmd_pending(a: argparse.Namespace) -> None:
    """Print the open escalation of a run once there is one."""
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        found = io().escalations(open_only=True, workflow_ids=[a.wid])
        if found:
            print(found[0]["escalation_id"])
            return
        time.sleep(1)
    sys.exit(3)


def cmd_escalation(a: argparse.Namespace) -> None:
    rows = io().escalations(open_only=False, workflow_ids=[a.wid])
    print(json.dumps(rows[0] if rows else None, default=str))


def cmd_markers(a: argparse.Namespace) -> None:
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
        exists = conn.execute("SELECT to_regclass('archi_scratch.marker')").fetchone()[0]
        print(conn.execute("SELECT count(*) FROM archi_scratch.marker").fetchone()[0] if exists else 0)


def cmd_mediation(a: argparse.Namespace) -> None:
    print(json.dumps(io().mediation(a.wid)))


def watermark_source() -> str:
    return f"{os.environ['ARCHI_OPS_DEPLOYMENT']}.{SOURCE.split('.')[1]}"  # okg names it <deployment>.<source>


def cmd_stale(a: argparse.Namespace) -> None:
    """Age (or refresh) the jira source's watermark: the scratch stand-in for a source that stopped updating."""
    interval = "now() - interval '30 days'" if a.state == "stale" else "now()"
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
        n = conn.execute(f"UPDATE okg.source_watermarks SET last_reconcile_at = {interval} WHERE source = %s",
                         (watermark_source(),)).rowcount
    print(n)
    if n != 1:
        sys.exit(f"expected to update one watermark row for {watermark_source()}, updated {n}")


def cmd_release_gate(a: argparse.Namespace) -> None:
    """Release a run held by test_gate_seconds."""
    from dbos import DBOSClient

    dsn = os.environ["OKG_DSN"]
    DBOSClient(database_url=dsn, system_database_url=dsn, application_database_url=dsn).send(
        a.wid, "release", "archi_ops_test_gate")
    print("released", a.wid)


def cmd_wait_log(a: argparse.Namespace) -> None:
    """Wait until the worker log holds `text` (after the byte offset of an earlier call)."""
    log = Path(a.log)
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        if a.text in log.read_text(errors="replace")[a.after:]:
            print("found")
            return
        time.sleep(1)
    sys.exit(3)


def cmd_tamper(a: argparse.Namespace) -> None:
    """Change the escalation row between the loop's decision and the mediator step (lab only)."""
    esc = io().escalations(open_only=False, workflow_ids=[a.wid])[0]
    patch = {"late": lambda row: {"decided_at": (datetime.fromisoformat(row["timeout_at"]) + timedelta(hours=1)).isoformat()},
             "superseded": lambda row: {"decision": {"verdict": "reject"}}}[a.kind](esc)
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
        n = conn.execute("UPDATE okg.escalation_live SET attrs = attrs || %s::jsonb "
                         "WHERE escalation_id = %s AND valid_to_gen IS NULL", (json.dumps(patch), esc["escalation_id"])).rowcount
    print(json.dumps({"updated": n, "patch": patch}))
    if n != 1:
        sys.exit(1)


def cmd_day_fill(a: argparse.Namespace) -> None:
    """Bring today's spend for the automation up to `target` (a ledger row; lab only)."""
    spent, cap = io().day_spend(AUTOMATION)
    day = datetime.now(timezone.utc).date().isoformat()
    scope = f"day:{AUTOMATION}:{day}"
    spent = spent or 0.0
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
        conn.execute("INSERT INTO okg.deployment_cost_ledger (scope, consumption_id, usd_consumed, workflow_id) "
                     "VALUES (%s, %s, %s, 'lab-day-fill')", (scope, f"lab-fill-{time.time_ns()}", a.target - spent))
    print(json.dumps({"scope": scope, "was": spent, "now": a.target, "cap": cap}))


def cmd_human_action(a: argparse.Namespace) -> None:
    """A person did what the shadow proposal said (an ops_action by `operator-x`), so shadow scoring can agree."""
    from archi.ops.record import OpsRecords, submit

    handle = io()
    proposal = handle.read_nodes("ops_proposal", handle.pin_generation(), {"record_id": f"prop-{a.wid}"})[0]
    attrs = proposal["attrs"]
    records = OpsRecords()
    records.carry(proposal)
    records.add("action", record_id=f"act-human-{a.wid}", proposal_id=attrs["record_id"], action_class=attrs["action_class"],
                payload=attrs["payload"], status="succeeded", result="done by hand",
                executed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                actor_kind="person", actor=OPERATOR, generation_read=attrs.get("generation_read"))
    _, dsn, deployment = env()
    print(json.dumps(submit(records, deployment=deployment, dsn=dsn, idempotency_key=f"human-{a.wid}")["output"]["status"]))


def cmd_pact(a: argparse.Namespace) -> None:
    """The PACT decisions recorded for changes whose id starts with the prefix."""
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
        rows = conn.execute("SELECT change_id, verdict, principal, note FROM okg.pact_decisions WHERE change_id LIKE %s "
                            "ORDER BY id", (a.prefix + "%",)).fetchall()
    print(json.dumps([dict(zip(("change_id", "verdict", "principal", "note"), r)) for r in rows]))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-instance").set_defaults(f=cmd_init_instance)
    c = sub.add_parser("configure")
    c.add_argument("--level")
    c.add_argument("--mode", choices=["shadow", "live"])
    c.add_argument("--timeout", type=int)
    c.add_argument("--proposal", choices=sorted(PROPOSALS))
    c.add_argument("--cost", type=float)
    c.add_argument("--estimate", type=float)
    c.add_argument("--gate", type=float, help="test_gate_seconds")
    c.set_defaults(f=cmd_configure)
    s = sub.add_parser("signal")
    s.add_argument("id")
    s.add_argument("--attempt", type=int, default=1)
    s.set_defaults(f=cmd_signal)
    w = sub.add_parser("wait")
    w.add_argument("wid")
    w.add_argument("--status")
    w.add_argument("--timeout", type=int, default=180)
    w.set_defaults(f=cmd_wait)
    pe = sub.add_parser("pending")
    pe.add_argument("wid")
    pe.add_argument("--timeout", type=int, default=180)
    pe.set_defaults(f=cmd_pending)
    for name, func in (("escalation", cmd_escalation), ("mediation", cmd_mediation), ("release-gate", cmd_release_gate),
                       ("human-action", cmd_human_action)):
        q = sub.add_parser(name)
        q.add_argument("wid")
        q.set_defaults(f=func)
    sub.add_parser("markers").set_defaults(f=cmd_markers)
    st = sub.add_parser("source")
    st.add_argument("state", choices=["stale", "fresh"])
    st.set_defaults(f=cmd_stale)
    wl = sub.add_parser("wait-log")
    wl.add_argument("log")
    wl.add_argument("text")
    wl.add_argument("--after", type=int, default=0)
    wl.add_argument("--timeout", type=int, default=120)
    wl.set_defaults(f=cmd_wait_log)
    t = sub.add_parser("tamper")
    t.add_argument("kind", choices=["late", "superseded"])
    t.add_argument("wid")
    t.set_defaults(f=cmd_tamper)
    d = sub.add_parser("day-fill")
    d.add_argument("target", type=float)
    d.set_defaults(f=cmd_day_fill)
    pa = sub.add_parser("pact")
    pa.add_argument("prefix")
    pa.set_defaults(f=cmd_pact)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
