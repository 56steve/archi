# Ticket triage

The `ticket-triage` automation reads new and updated tickets from Redmine, JIRA or ServiceNow,
whichever this instance ingests (JIRA through the `jira` pack or the `tickets` pack's generic JIRA
reader). For each one it writes a cited triage draft. At L2 it can also propose
one change to the ticket, which happens only after a person approves it. Every proposal is recorded in
the operational record (`ops_proposal`), whatever it proposes.

## What starts a run

A ticket whose `updated` time is newer than the last one the automation saw, once a published generation
holds it. Closed tickets are skipped. `queues:` in `ops.yaml` limits it to some projects (ServiceNow:
assignment groups). The tickets already in the queue on the day the automation is installed are not
triaged.

## What a run reads, at its pinned generation only

1. The ticket, by its key.
2. Related tickets: okg's search over the same system, ranked by the ticket's subject and the start of
   its description. Closed ones show how they ended.
3. Precedents: ops-record incidents on the ticket or a related ticket, and what was proposed and decided
   for them (see `ops_precedents.md`).
4. Documentation: the document subtypes `proposer.doc_subtypes` names (by default ServiceNow knowledge
   articles and documentation pages).

A proposal may cite only what this read returned. Each claim in a draft carries a `[n]` that points at a
node id under **Sources**.

## What it proposes

| Level | Proposal | What happens |
|---|---|---|
| L0 | the triage draft | recorded only |
| L1 (default) | the triage draft: category, likely component, related tickets, precedents, documentation, and a suggested reply | recorded on the proposal for a person to send; nothing is sent |
| L2 | `assign_ticket` if the ticket is unassigned and most related tickets went to the same person (ServiceNow: group); else `set_ticket_status` to `triaged_status` for a ticket in one of `triage_from`; else the draft | an escalation waits for a person; only an approval runs the live tool, which notes the approval on the ticket. A rejected or timed-out proposal never runs |

Every automation starts in **shadow**: it proposes and records, opens no escalation and changes nothing,
until an operator promotes it (`archi ops promote ticket-triage`, then an approved PACT change).

## Reading what it did

- `archi ops status`: level, mode, budget and shadow scores.
- `archi ops pending`, then `archi ops approve <escalation> --as <you>` or `archi ops reject`.
- The draft of a run is the `draft` of its `ops_proposal` (`okg filter --type ops_proposal --where
  attrs.automation=ticket-triage`).
- `archi ops halt` stops every automation on the instance; `archi ops resume` starts them again.

## Raising it to L2

Set `automations.ticket-triage.level: L2` in `archi.yaml`, and in `ops.yaml` give each system its API and
the names of its credential variables:

```yaml
automations:
  ticket-triage:
    proposer:
      kind: ticket-triage
      systems:
        redmine: {url: https://redmine.example.org, token_env: REDMINE_API_KEY, triaged_status: In Progress, triage_from: [New]}
        jira: {url: https://jira.example.org, token_env: JIRA_TOKEN, triaged_status: In Progress}
        servicenow: {url: https://example.service-now.com, user_env: SERVICENOW_USERNAME, password_env: SERVICENOW_PASSWORD}
```

The worker's environment holds the variables; archi never writes their values anywhere. Without a
system's `url` the run drafts and its summary says why.
