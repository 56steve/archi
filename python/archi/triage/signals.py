"""New and updated tickets become signals (the scanner's half of a `ticket_queue` trigger).

`archi.ops.workflow.scan_once` calls `add_ticket_signals` on every scan, before it starts runs. For each
automation whose trigger is `{type: ticket_queue, system: [...], queue: ...}` it reads each system's
tickets from the latest published generation and adds one signal per new or updated open ticket:

    signal id   <system>-<key>-<updated, UTC>   (archi.triage.tickets.signal_id)
    entity      <system>:<key>
    source      ticket

A system's tickets are read from every subtype that holds them and whose pack the instance selected (the
automation's gate sources name those packs): JIRA from the `jira` pack's `jira_issue`, from the `tickets`
pack's `tracker_ticket` rows with system `jira`, or both. The same JIRA issue in both makes one signal.

What "new or updated" means. Each automation keeps a cursor per system (`archi_ops.ticket_cursors`): the
newest `updated` time it has seen and the generation it read. A system is read again only when a newer
generation is published. A ticket becomes a signal when its `updated` is after the cursor, it is not
closed, and its queue is among the instance's `queues` (ops.yaml; unset means every queue). The first read
of a system only sets the cursor: the tickets already in the queue when the automation is installed are not
triaged, so installing it does not start a backlog of runs. The signal id holds the update time, so a
ticket updated twice is triaged twice, and adding a signal that exists already changes nothing.

Known limit: a ticket update made by this automation's own L2 action (a status or assignment change) is a
new update, so the ticket is triaged once more; that run finds nothing left to propose and drafts.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from archi.bundles.automation import trigger_systems
from archi.triage.tickets import CLOSED_STATUSES, SYSTEMS, Ticket, closed_statuses, signal_id, subtypes_for, tickets_of

log = logging.getLogger(__name__)


def new_or_updated(tickets: Iterable[Ticket], since: Optional[datetime], queues: Iterable[str] = ()
                   ) -> tuple[list[Ticket], Optional[datetime]]:
    """(the tickets to triage, oldest update first; the new cursor). With no cursor (`since` None) nothing
    is triaged and the cursor becomes the newest update seen."""
    tickets = [t for t in tickets if t.updated_at is not None]
    newest = max((t.updated_at for t in tickets), default=None)
    mark = max((m for m in (since, newest) if m is not None), default=None)
    if since is None:
        return [], mark
    wanted = set(queues)
    fresh = [t for t in tickets if t.updated_at > since and not t.closed and (not wanted or t.queue in wanted)]
    return sorted(fresh, key=lambda t: (t.updated_at, t.entity)), mark


def packs_of(automation: Any) -> Optional[set[str]]:
    """The packs an automation's gate sources name (`<pack>.<source>`); None when it has no gate."""
    gate = getattr(automation, "gate", None)
    return {s.split(".", 1)[0] for s in gate.sources} if gate is not None and gate.sources else None


def scan_queue(io: Any, automation: str, systems: Iterable[str], *, queues: Iterable[str] = (),
               packs: Optional[Iterable[str]] = None, closed_statuses: frozenset[str] = CLOSED_STATUSES,
               say: Callable[[str], None] = log.info) -> list[str]:
    """Read each system's tickets at the latest published generation and add their signals. Returns the
    signal ids it added, as `<automation>/<id>`."""
    generation = io.pin_generation()
    added: list[str] = []
    rows: dict[str, list] = {}  # one read per subtype per scan (tracker_ticket holds two systems)
    for system in systems:
        cursor = io.ticket_cursor(automation, system)
        if cursor is not None and cursor["generation"] == generation:
            continue
        tickets: list[Ticket] = []
        skipped: list[dict] = []
        for subtype in subtypes_for(system, packs):
            if subtype not in rows:
                rows[subtype] = io.read_nodes(subtype, generation)
            tickets += tickets_of(system, rows[subtype], closed_statuses=closed_statuses, skipped=skipped)
        for s in skipped:
            say(f"archi_ops_scan: {automation}: skipped {s['what']}: {s['reason']}")
        fresh, mark = new_or_updated(tickets, cursor["updated"] if cursor else None, queues)
        for ticket in fresh:
            sid = signal_id(ticket)
            if f"{automation}/{sid}" in added:
                continue  # the same issue from two packs
            io.add_signal(automation, sid, ticket.entity, f"{ticket.system} {ticket.key}: {ticket.subject}"[:300],
                          summary=f"status {ticket.status or '-'}; queue {ticket.queue or '-'}; updated {ticket.updated}",
                          source="ticket")
            added.append(f"{automation}/{sid}")
        io.set_ticket_cursor(automation, system, mark, generation)
        if cursor is None:
            say(f"archi_ops_scan: {automation}: first read of {system} ({len(tickets)} tickets); "
                "triage starts with the next new or updated ticket")
    return added


def add_ticket_signals(rt: Any, io: Any, say: Callable[[str], None] = log.info) -> list[str]:
    """`scan_queue` for every automation with a `ticket_queue` trigger. A failure stops that automation's
    signals for this scan (it is logged), not the scan."""
    load = getattr(rt, "automation", None)
    if load is None:
        return []
    added: list[str] = []
    for name in sorted(rt.settings):
        try:
            auto = load(name)
            trigger = auto.trigger
            if trigger.get("type") != "ticket_queue":
                continue
            systems = [s for s in trigger_systems(trigger) if s in SYSTEMS]
            settings = rt.settings[name]
            added += scan_queue(io, name, systems, queues=getattr(settings, "queues", ()), packs=packs_of(auto),
                                closed_statuses=closed_statuses(settings.proposer), say=say)
        except Exception as exc:  # a bad automation file or an unreadable graph stops this automation, not the scan
            say(f"archi_ops_scan: {name}: cannot read its ticket queue ({type(exc).__name__}: {exc})")
    return added
