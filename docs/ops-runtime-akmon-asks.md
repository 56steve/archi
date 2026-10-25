# AKMON asks from the operations runtime (drafts)

Status: **drafts, not filed.** Jason files them on `mitdbg/akmon`. After
filing, open one `upstream-ask` issue on `archi-physics/archi` per ask that
links it (ADR 0002 §9), and record the links as `manual` evidence on
`req.r3-ops-runtime.upstream` in PACT `r3-ops-runtime`.

Asks O1–O6 are the six ADR 0002 §9 item 7 names. O7–O11 were found while
building the loop on okg v0.2.0. Each says whether it was **proven** by
running code on a scratch instance (submit76, okg 0.2.0, 2026-10-25) or
**read** from okg's source only. Each names the archi code that goes away
when it lands (ADR 0002 D7).

Until these land, automations run co-located as deployment workflows,
edits are handled as reject-and-resubmit, and `archi ops halt` wraps
pause and cancel (ADR 0002 §9).

---

## O1. Expiry on admission envelopes

**Problem (read).** An admission envelope has no expiry: there is no
`expires` or `ttl` in `okg/substrate/admission.py` or
`admission_workflows.py`. A proposal admitted as a record stays actionable
forever. Escalations do carry `timeout_at`, which archi uses for L2, but an
envelope submitted outside an escalation cannot say "void after T".

**Ask.** An optional `expires_at` on an admission envelope. okg refuses to
admit it, and refuses any decision on it, after that time.

**Acceptance.** An envelope submitted with `expires_at` in the past is
refused with a distinct status. A decision after `expires_at` is refused.

**archi deletes.** Nothing yet. Archi's L2 path relies on escalation
timeouts plus its own deadline re-check (O8).

## O2. Approve with edits

**Problem (read).** An approver can only approve or reject. An operator
who would approve "restart the door, but on the other host" must reject,
and the automation must propose again.

**Ask.** A decision that approves with an edited payload. okg records the
original and the edited payload, and the workflow receives the edited one.

**Acceptance.** Approve-with-edits on an escalation delivers the edited
payload to the waiting workflow and records both in the escalation row.

**archi deletes.** The reject-and-resubmit note in `archi ops approve`'s
help. The loop already validates a payload against the action class's
schema, so an edited payload goes through the same check.

## O3. A mounted, authenticated proposal API

**Problem (proven and read).** Escalations can only be listed and answered
from a shell on the deployment's host:
- There is no `okg runtime escalation list` or `show`.
- There is no HTTP route.
- `decided_by` is a free string that nothing authenticates.

Archi adds `archi ops pending`, which reads `okg.escalation_live` with SQL.

**Ask.** An authenticated API (and the CLI over it) to list, show and
answer escalations, which records the authenticated caller as the decider.

**Acceptance.** An escalation answered through the API records an
authenticated `decided_by`; an unauthenticated call is refused.

**archi deletes.** The SQL read in `archi ops pending` and the OS-user
default for `decided_by` in `archi ops approve` and `reject`.

## O4. Per-actor cost attribution on `llm_calls` and `mcp_call_log`

**Problem (read).** `llm_calls` has a `caller` field and an `extras` JSON
column, but no actor column, and the SDK's `llm_call` does not write
`llm_calls` at all. `deployment_cost_ledger` has a `workflow_id`. An
operator cannot ask "what did automation X spend this week" from okg.

**Ask.** An actor column (automation or person, plus id) on `llm_calls`
and `mcp_call_log`, written by `llm_call` and the MCP server.

**Acceptance.** An `llm_call` inside a workflow writes a row with the
workflow's actor; a per-actor sum over a day matches the cost ledger.

**archi deletes.** The cost fields archi copies onto its own ops records.

## O5. A first-class kill switch

**Problem (proven).** okg has no single "stop everything" for a
deployment's workflows:
- `pause-schedule` stops cron ticks, but `trigger-schedule` still starts
  runs on a paused schedule, and missed ticks are backfilled on resume
  unless the schedule turned backfill off.
- `cancel-workflow` acts only at the next step boundary. It does not wake
  a workflow waiting in `recv`, and it leaves that workflow's escalation
  `pending`. A later approval then marks it `applied` for a cancelled run.
- SIGTERM does not stop a worker while a run waits.

**Ask.** One command, and one API call, that stops a deployment's
workflows:
- it refuses new runs, manual triggers included;
- it cancels runs in flight, waking any `recv`;
- it closes their open escalations as cancelled;
- the worker exits promptly on SIGTERM.

**Acceptance.** With a run waiting on an escalation, the kill switch
leaves:
- the run CANCELLED;
- the escalation closed;
- `trigger-schedule` refused;
- no step running after the call returns.

**archi deletes.** In `archi ops halt`: the pause, close-escalations and
cancel sequence; and the halt check at the top of the mediator step,
once cancel is synchronous.

## O6. A retraction-size guard (okg#3307)

**Problem (read).** Admission refuses retractions outright; a source that
needs to retract many records has no guarded path.

**Ask.** As okg#3307: retractions allowed up to a declared size, with a
guard that refuses a retraction larger than the bound.

**Acceptance.** As written in okg#3307.

**archi deletes.** Nothing; ops records do not retract today.

## O7. Bug: `okg runtime escalation respond` always fails

**Problem (proven).** Every call raises `ImportError`.
`okg/substrate/cli/runtime.py:1154` imports `_current_user` from
`okg.substrate.cli.main`, but it is defined in `okg/substrate/cli/view.py:422`.
`--decided-by` does not help, because the import is unconditional.

**Ask.** Fix the import.

**Acceptance.** `okg runtime escalation respond <id> --decision '{...}'`
answers a pending escalation.

**archi deletes.** Archi's direct call to
`okg.substrate.runtime.escalation_response.respond_to_escalation` in
`archi ops approve` and `reject`.

## O8. `respond_to_escalation` accepts a decision after the deadline

**Problem (proven).** With the worker stopped past an escalation's
`timeout_at`, `respond_to_escalation` accepted an approval 14 s late.
After the worker restarted, the waiting `recv` returned it and the action
ran. A timed-out L2 proposal must never execute (ADR 0002 §7.3).

**Ask.** Refuse a decision when `now > timeout_at`, and mark the
escalation `timed_out` at that point.

**Acceptance.** Respond after `timeout_at` raises `EscalationStateError`
whether or not the worker is running.

**archi deletes.** Nothing. Archi keeps its own deadline re-check inside
the mediator step as defence in depth, but it stops being the only
defence.

## O9. A public way to bind a workflow's deployment context

**Problem (proven).** A plain `@deployment_workflow` has no deployment
context or connection, so `request_escalation` and `cost_budget` fail.
They work after `bind_sdk_context(deployment=, conn=)` from
`okg.deployment._internal.context`, which is private.

**Ask.** Bind the deployment context for every `@deployment_workflow`
and step, or make a binding function public and documented.

**Acceptance.** `request_escalation` and `cost_budget` work in a
`@deployment_workflow` with no private import.

**archi deletes.** The private import, and archi's own connection, in
`archi.ops.workflow`.

## O10. `on_escalation_decided` never fires

**Problem (read; checked twice, not run).** okg exports an
`@on_escalation_decided` decorator and defines the `escalation_decided`
hook event (`okg/substrate/runtime/hooks.py:71`). But the only caller of
`enqueue_hooks` is the publisher (`okg/substrate/publisher/publisher.py:7375`),
for generation events. Nothing enqueues the escalation event.

**Ask.** Call the hook when an escalation is decided, or remove it.

**Acceptance.** A registered hook runs once per decision.

**archi deletes.** Nothing yet. Archi would use the hook to notify the
approver's ticket.

## O11. Changing `runtime:` after install blocks publishing

**Problem (proven).** Adding `runtime: {enabled: true, workflows: [...]}`
to `deployment.yaml` after `okg install` makes every publisher cycle
refuse with `CatalogOwnershipMismatch` (`drifted_blocks=['runtime']`) until
`okg catalog load --apply` runs.

**Ask.** Treat a `runtime:` change as not part of catalog ownership, or
have `okg add` and `okg deployment` commands apply it with the claim.

**Acceptance.** Editing `runtime.workflows`, committing and publishing
works without a separate catalog load.

**archi deletes.** The rule that archi's renderer writes `runtime:` before
`okg install`, and the claim step `archi add` runs when it adds an
automation pack.

## O12. Bootstrap the DBOS schema before install's ingest when the runtime is enabled

**Problem (proven, submit76, okg 0.2.0, 2026-10-25).** With `runtime.enabled: true` in
`deployment.yaml` from the start, `okg install` (without `--no-publish`) stops at its last stage
(`ingest_failed`): the ingest enqueues a DBOS workflow, and `dbos.workflow_status` does not exist
until `okg runtime bootstrap` has run. Every earlier stage has finished. `okg install --no-publish`
followed by `okg provision` does finish. Even then ingest defaults to DBOS-enqueue when the runtime is
enabled, so an `okg ingest` with no worker up waits for one; `--inline` avoids it.

**Ask.** `install` bootstraps the runtime schema before the ingest stage, and every ingest runs in-process
when no worker is up.

**Acceptance.** `okg install` on a profile whose deployment defaults enable the runtime finishes
without a hand step and without a worker.

**archi deletes.** `archi.cli.init.provision`'s recovery (`okg runtime bootstrap`, `okg ingest
--inline`, `okg run --once --apply`) and the matching call in `archi up`.

## O13. A queued admission must not block the next worker start

**Problem (proven).** An `okg admission submit` made while no worker runs leaves an `ENQUEUED`
`okg_admission_submit` workflow with no application version. The next `okg runtime worker` then
refuses to start: `RuntimeCutoverBlocked: DBOS application-version cutover is blocked by active
workflow history that is not safely replaceable`.

**Ask.** Stamp a client-enqueued admission with the version the worker will run it under, or let a
worker adopt it.

**Acceptance.** Submit an admission with the worker down, start the worker: the admission runs and
the worker is ready.

**archi deletes.** `OpsIO.cancel_unconfirmed_admission` and the halt path that cancels its own
unconfirmed admission (the halted record is not written when the worker is down).
