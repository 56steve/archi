"""Helpers for scratch_ops_runtime_run.sh: configure the instance, add signals, wait on runs,
look at escalations, markers and watermarks. Scratch use only.

    python scratch_ops_runtime_tools.py <command> ...

Environment: ARCHI_INSTANCE, OKG_DSN, ARCHI_OPS_DEPLOYMENT.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg
import yaml

from archi.ops.okg_io import OpsIO

AUTOMATION = "scratch-triage"
SEED_INCIDENT = "ops_incident:inc-seed-1"
SEED_ENTITY = "T2_US_MIT"
SOURCE = "jira.jira"

PROPOSALS = {
    "marker": {
        "summary": "restart the SRM service at the site",
        "evidence_ids": [SEED_INCIDENT],
        "action_class": "write_marker",
        "payload": {"entity": SEED_ENTITY, "note": "restart srm"},
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


def cmd_configure(a: argparse.Namespace) -> None:
    """Write archi.yaml (the level) and ops.yaml (mode, timeout, stub proposal and cost)."""
    inst, _, deployment = env()
    (inst / "archi.yaml").write_text(yaml.safe_dump(
        {"name": deployment, "automations": {AUTOMATION: {"level": a.level}}}), encoding="utf-8")
    (inst / "ops.yaml").write_text(yaml.safe_dump({"automations": {AUTOMATION: {
        "mode": a.mode, "timeout_seconds": a.timeout, "estimate_cost": 0.05,
        "proposer": {"kind": "stub", "fixture": {"default": PROPOSALS[a.proposal], "cost": a.cost}},
        "outcome": {"kind": "marker"}}}}), encoding="utf-8")


def cmd_signal(a: argparse.Namespace) -> None:
    io().add_signal(AUTOMATION, a.id, SEED_ENTITY, f"SRM timeouts again ({a.id})")
    print(f"{AUTOMATION}-{a.id}-1")


def run_status(conn, wid: str):
    return conn.execute("SELECT status, output FROM dbos.workflow_status WHERE workflow_uuid = %s", (wid,)).fetchone()


def cmd_wait(a: argparse.Namespace) -> None:
    """Wait until the run is final (SUCCESS, ERROR, CANCELLED) or `--status` is reached."""
    deadline = time.time() + a.timeout
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
        while time.time() < deadline:
            row = run_status(conn, a.wid)
            if row and (row[0] == a.status if a.status else row[0] in ("SUCCESS", "ERROR", "CANCELLED")):
                print(json.dumps({"status": row[0], "output": row[1]}))
                return
            time.sleep(1)
    row = None
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
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


def cmd_stale(a: argparse.Namespace) -> None:
    """Age (or refresh) the jira source's watermark: the scratch stand-in for a source that stopped updating."""
    interval = "now() - interval '30 days'" if a.state == "stale" else "now()"
    with psycopg.connect(os.environ["OKG_DSN"], autocommit=True) as conn:
        cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='okg' "
            "AND table_name='source_watermarks'").fetchall()]
        print("source_watermarks columns:", cols, file=sys.stderr)
        n = conn.execute(f"UPDATE okg.source_watermarks SET last_reconcile_at = {interval} "
                         "WHERE source_name = %s", (SOURCE,)).rowcount
        print(n)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("configure")
    c.add_argument("--level", required=True)
    c.add_argument("--mode", default="live")
    c.add_argument("--timeout", type=int, default=120)
    c.add_argument("--proposal", default="marker", choices=sorted(PROPOSALS))
    c.add_argument("--cost", type=float, default=0.1)
    c.set_defaults(f=cmd_configure)
    s = sub.add_parser("signal")
    s.add_argument("id")
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
    e = sub.add_parser("escalation")
    e.add_argument("wid")
    e.set_defaults(f=cmd_escalation)
    sub.add_parser("markers").set_defaults(f=cmd_markers)
    st = sub.add_parser("source")
    st.add_argument("state", choices=["stale", "fresh"])
    st.set_defaults(f=cmd_stale)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
