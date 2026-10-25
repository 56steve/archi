"""Promotion of an automation from shadow to live (PACT r3-ops-runtime req.shadow; ADR 0002 section 7.3).

"Promotion to live needs recorded shadow results and an operator's approval, recorded as a PACT
decision on the instance." This module does the repository half of `archi ops promote`:

1. refuse unless the shadow report passes `archi.ops.shadow.promotion_readiness` (the caller passes
   both, so the thresholds an operator asked for are in the PACT note);
2. refuse unless the instance directory is a git repository;
3. `okg pact new promote-<automation>-<time> --shape fast` in the instance repo, with the report saved
   beside it and a junit file for its one test (the readiness check), ingested as the change's evidence;
   `okg pact gate task-done` then brings the change to awaiting_review, the one state where approve is legal;
4. `okg pact decide <change> --verdict approve --principal <operator> --note <report summary>`;
5. set the automation live in `ops.yaml` (`mode: live`, `promoted_at: <now>`);
6. commit the change directory and `ops.yaml`, by name.

`promoted_at` is what lifts a recorded demotion: a demotion newer than it keeps the automation in
shadow (archi.ops.loop). Step 4 writes okg's PACT ledger (okg.pact_decisions in the graph database).
Nothing is written before every refusal check has passed; if `okg pact` fails, `ops.yaml` is untouched
and nothing is committed (the half-made change directory is left for the operator to look at).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import yaml

from archi.ops.record import _okg
from archi.ops.runtime_config import OPS_FILE


class PromotionRefused(RuntimeError):
    """The automation cannot be promoted now; the message says why."""


Runner = Callable[..., subprocess.CompletedProcess]


def change_id(automation: str, now: datetime) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", automation.lower()).strip("-")
    return f"promote-{slug}-{now.astimezone(timezone.utc):%Y%m%dt%H%M%Sz}"


def summary(report: Mapping[str, Any]) -> str:
    """One line of the report for the PACT note."""
    keys = ("scored", "answered", "agreement_rate", "response_coverage", "false_proposal_rate_upper")
    return ", ".join(f"{k}={report.get(k)}" for k in keys)


def _git(directory: Path, *args: str, run: Runner) -> subprocess.CompletedProcess:
    """git in the instance repo, committing as archi does (`archi init` sets no user in the repo)."""
    from archi.cli.init import _git_identity_env

    return run(["git", "-C", str(directory), *args], capture_output=True, text=True,
               env={**os.environ, **_git_identity_env()})


def promote(
    instance_dir: Path,
    automation: str,
    *,
    report: Mapping[str, Any],
    readiness: Mapping[str, Any],
    principal: str,
    note: str = "",
    dsn: Optional[str] = None,
    deployment: Optional[str] = None,
    now: Optional[datetime] = None,
    run: Runner = subprocess.run,
    okg: Optional[str] = None,
) -> dict[str, Any]:
    """Promote `automation`; see the module docstring. Raises PromotionRefused."""
    instance_dir = Path(instance_dir)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not readiness.get("ready"):
        raise PromotionRefused(
            f"{automation} is not ready to promote: " + "; ".join(readiness.get("reasons") or ["no shadow report"]))
    inside = _git(instance_dir, "rev-parse", "--is-inside-work-tree", run=run)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise PromotionRefused(f"{instance_dir} is not a git repository; the promotion decision is recorded in "
                               "the instance repo, so it must be one")
    ops_path = instance_dir / OPS_FILE
    data = yaml.safe_load(ops_path.read_text(encoding="utf-8")) if ops_path.is_file() else {}
    data = data or {}
    if automation not in (data.get("automations") or {}):
        raise PromotionRefused(f"{ops_path} has no automations.{automation}; there is nothing to promote")

    change = change_id(automation, now)
    exe = okg or _okg()
    text = (f"Promote automation {automation} from shadow to live. Shadow report: {summary(report)}."
            + (f" Operator note: {note}" if note else ""))
    pact_env = {**os.environ, "OKG_PACT_QUIET_NUDGE": "1"}
    if deployment:
        pact_env["OKG_PACT_GRAPH_DEPLOYMENT"] = deployment
        pact_env.setdefault("OKG_DEPLOYMENTS_DIR", str(instance_dir / "deployments"))
    if dsn:
        pact_env["OKG_DSN"] = dsn
    change_dir = instance_dir / "pact" / "changes" / change
    rel = f"pact/changes/{change}"
    test_id = "promotion/shadow_report.py::test_promotion_readiness"

    def pact(*args: str, what: str) -> None:
        done = run([exe, "pact", "--root", str(instance_dir), *args], capture_output=True, text=True,
                   env=pact_env, cwd=str(instance_dir))
        if done.returncode != 0:
            detail = "\n".join(line for line in ((done.stderr or "") + (done.stdout or "")).splitlines()
                               if line.strip() and not line.startswith("tip:"))[-1500:]
            raise PromotionRefused(f"okg pact {what} failed; {automation} stays in shadow and nothing was "
                                   f"committed: {detail}")

    # The change is a small PACT whose one test is the readiness check; its junit report is the evidence,
    # so the change reaches awaiting_review and the operator's approval is a legal verdict.
    pact("new", change, "--shape", "fast", "--title", f"Promote {automation} to live", "--intent", text,
         "--acceptance", "The shadow report passes promotion_readiness and the operator approves.",
         "--task-title", f"Promote {automation}", "--test-id", test_id,
         "--test-command", f"archi ops promote {automation}", what="new")
    (change_dir / "shadow-report.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str),
                                                   encoding="utf-8")
    (change_dir / "readiness.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<testsuites><testsuite name="archi-ops-promotion" tests="1" '
        'failures="0" errors="0"><testcase classname="promotion.shadow_report" name="test_promotion_readiness"/>'
        "</testsuite></testsuites>\n", encoding="utf-8")
    pact("evidence", "ingest", change, "--pytest-report", f"{rel}/readiness.xml", what="evidence ingest")
    pact("gate", "task-done", change, f"task.{change}", what="gate task-done")
    decide = ["decide", change, "--verdict", "approve", "--principal", principal, "--note", text]
    if dsn:
        decide += ["--dsn", dsn]
    pact(*decide, what="decide")

    data["automations"][automation] = {**(data["automations"][automation] or {}), "mode": "live",
                                       "promoted_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    ops_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    rel_change = rel
    added = _git(instance_dir, "add", "--", OPS_FILE, rel_change, run=run)
    committed = _git(instance_dir, "commit", "-q", "-m", f"promote {automation} to live ({change})", "--",
                     OPS_FILE, rel_change, run=run)
    if added.returncode != 0 or committed.returncode != 0:
        raise PromotionRefused("the decision is recorded and ops.yaml says live, but the commit failed: "
                               f"{(committed.stderr or added.stderr or committed.stdout)[-1500:]}")
    return {"automation": automation, "change": change, "promoted_at": data["automations"][automation]["promoted_at"],
            "principal": principal, "report": summary(report)}
