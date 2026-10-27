"""Live tools: change a ticket's status or assignment in Redmine, JIRA or ServiceNow.

These are the mediators of the ticket-triage action classes `set_ticket_status` and `assign_ticket`
(`bundles/packs/ticket-triage/action_classes.yaml`). Each changes one field of one ticket in an external
system, so it runs at L2 only: the ops runtime calls it after a person approved the escalation, and the
tool checks that approval again itself before it sends anything (`archi.tools._live.require_approval`:
the approval must be for this proposal, this action class and this exact payload). Then:

1. it reads the ticket. When the field already holds the requested value it changes nothing and succeeds
   (a repeat with the same idempotency key, or a person who got there first);
2. when the field no longer holds what it held at proposal time (`expected_status`, `expected`), it
   changes nothing and fails: the proposal is stale;
3. it writes the field with a note on the ticket that names the approver, the escalation and the proposal,
   so the ticket's own history shows the decision the escalation ledger holds;
4. it reads the ticket again and fails unless the field now holds the value.

A write that times out or loses its connection may have applied, so it returns `unknown`, never `failed`.

    set_ticket_status(payload={system, ticket, [table], status, expected_status, note, endpoint}, context=...)
    assign_ticket(payload={system, ticket, [table], field, value, expected, note, endpoint}, context=...)

`endpoint` is `{url, token_env | user_env + password_env}`: the API base URL and the names of the
environment variables that hold the credential (the values are read here, never put in a payload, a log,
an error or a result). The URL must be https; plain http is accepted only for a loopback host (a local
proxy or a test server), where the credential does not leave the machine. Redirects are not followed.

    redmine     X-Redmine-API-Key from token_env. Status by name (GET /issue_statuses.json); assignee by
                name among the project's members (GET /projects/<id>/memberships.json).
    jira        Bearer token_env (a personal access token) or basic user_env/password_env. Status by a
                transition whose target has that name; assignee by user search, exactly one match.
    servicenow  basic user_env/password_env, or Bearer token_env. Fields are written as display values
                (state name, group or person name), in the table the payload names.

The HTTP session is a seam: any object with `request(method, url, headers=, json=, params=, timeout=,
allow_redirects=)` returning a response with `status_code`, `json()` and `text` (a `requests.Session`
does). The unit tests and the scratch run pass their own and never reach a real ticket system.
"""
from __future__ import annotations

import base64
import os
import re
from typing import Any, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit

from archi.tools._live import ActRefused, ToolError, Verifier, outcome, require_approval, validate_payload

__all__ = ["ActRefused", "ToolError", "PAYLOAD_SCHEMAS", "TOOLS", "set_ticket_status", "assign_ticket"]

TIMEOUT = 30
LOOPBACK = ("127.0.0.1", "localhost", "::1")
#: a ticket key as each system writes it: a Redmine id, a JIRA key, a ServiceNow number
KEY_PATTERNS = {
    "redmine": re.compile(r"[0-9]{1,12}"),
    "jira": re.compile(r"[A-Z][A-Z0-9_]{0,31}-[0-9]{1,9}"),
    "servicenow": re.compile(r"[A-Z]{2,8}[0-9]{4,12}"),
}
ONE_LINE = r"^[^\x00-\x1f\x7f]+$"

_ENDPOINT = {
    "type": "object",
    "required": ["url"],
    "additionalProperties": False,
    "properties": {
        "url": {"type": "string", "pattern": r"^https?://[^\s/?#]+(/[^\s?#]*)?$"},
        "token_env": {"type": "string", "pattern": "^[A-Z_][A-Z0-9_]*$"},
        "user_env": {"type": "string", "pattern": "^[A-Z_][A-Z0-9_]*$"},
        "password_env": {"type": "string", "pattern": "^[A-Z_][A-Z0-9_]*$"},
    },
}
_COMMON = {
    "system": {"enum": ["redmine", "jira", "servicenow"]},
    "ticket": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"},
    "table": {"enum": ["incident", "sc_request"]},
    "note": {"type": "string", "minLength": 1, "maxLength": 4000},
    "endpoint": _ENDPOINT,
}

PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {
    "set_ticket_status": {
        "type": "object",
        "required": ["system", "ticket", "status", "expected_status", "note", "endpoint"],
        "additionalProperties": False,
        "properties": {
            **_COMMON,
            "status": {"type": "string", "minLength": 1, "maxLength": 100, "pattern": ONE_LINE},
            "expected_status": {"type": "string", "maxLength": 100},
        },
    },
    "assign_ticket": {
        "type": "object",
        "required": ["system", "ticket", "field", "value", "expected", "note", "endpoint"],
        "additionalProperties": False,
        "properties": {
            **_COMMON,
            "field": {"enum": ["assignee", "assignment_group"]},
            "value": {"type": "string", "minLength": 1, "maxLength": 200, "pattern": ONE_LINE},
            "expected": {"type": "string", "maxLength": 200},
        },
    },
}

#: What the pack ships, in one place: the tests hold this equal to pack.yaml and action_classes.yaml.
TOOLS: dict[str, dict[str, Any]] = {
    "set_ticket_status": {"kind": "act", "idempotent": True},
    "assign_ticket": {"kind": "act", "idempotent": True},
}


class TicketToolError(RuntimeError):
    """The ticket system refused or could not be read. Carries no credential."""


class WriteUnknown(RuntimeError):
    """A write whose answer never came: it may have applied."""


def _same(a: Any, b: Any) -> bool:
    return str(a or "").strip().casefold() == str(b or "").strip().casefold()


def _base_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK):
        return url.rstrip("/")
    raise ToolError(f"endpoint url must be https (plain http only to a loopback host); got {parts.scheme}://{parts.hostname}")


def _display(value: Any) -> str:
    """A field ServiceNow or JIRA returns as a string or as an object with a display name."""
    if isinstance(value, Mapping):
        for key in ("display_value", "displayName", "name", "value"):
            if value.get(key):
                return str(value[key])
        return ""
    return "" if value is None else str(value)


class _Client:
    def __init__(self, endpoint: Mapping[str, Any], env: Mapping[str, str], session: Any):
        self.base = _base_url(str(endpoint["url"]))
        self.headers = {"Accept": "application/json", "Content-Type": "application/json", **self.auth(endpoint, env)}
        if session is None:
            import requests

            session = requests.Session()
        self.session = session

    @staticmethod
    def _need(env: Mapping[str, str], name: Optional[str], what: str) -> str:
        if not name:
            raise ToolError(f"endpoint names no {what} variable")
        value = env.get(name)
        if not value:
            raise ToolError(f"environment variable {name} ({what}) is not set")
        return value

    def basic_or_bearer(self, endpoint: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, str]:
        if endpoint.get("token_env"):
            return {"Authorization": f"Bearer {self._need(env, endpoint['token_env'], 'token')}"}
        user = self._need(env, endpoint.get("user_env"), "user")
        password = self._need(env, endpoint.get("password_env"), "password")
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}

    def auth(self, endpoint: Mapping[str, Any], env: Mapping[str, str]) -> dict[str, str]:
        return self.basic_or_bearer(endpoint, env)

    def call(self, method: str, path: str, *, json: Any = None, params: Optional[Mapping[str, Any]] = None,
             write: bool = False) -> Any:
        try:
            response = self.session.request(method, self.base + path, headers=self.headers, json=json,
                                            params=params, timeout=TIMEOUT, allow_redirects=False)
        except Exception as exc:  # timeout, connection reset: a write may have applied
            if write:
                raise WriteUnknown(f"{method} {path}: {type(exc).__name__}") from None
            raise TicketToolError(f"{method} {path}: {type(exc).__name__}") from None
        if response.status_code >= 300:
            raise TicketToolError(f"{method} {path}: HTTP {response.status_code}: {str(response.text)[:200]}")
        if response.status_code == 204 or not str(response.text or "").strip():
            return None
        return response.json()


class Redmine(_Client):
    def auth(self, endpoint, env):
        return {"X-Redmine-API-Key": self._need(env, endpoint.get("token_env"), "token")}

    def read(self, key: str, table: Optional[str]) -> dict[str, str]:
        issue = (self.call("GET", f"/issues/{quote(key)}.json") or {}).get("issue") or {}
        return {"status": _display(issue.get("status")), "assignee": _display(issue.get("assigned_to")),
                "project": str((issue.get("project") or {}).get("id") or "")}

    def set_status(self, key, table, current, status, note):
        statuses = (self.call("GET", "/issue_statuses.json") or {}).get("issue_statuses") or []
        found = [s for s in statuses if _same(s.get("name"), status)]
        if len(found) != 1:
            raise TicketToolError(f"Redmine has {len(found)} statuses named {status!r}, not one")
        self.call("PUT", f"/issues/{quote(key)}.json", json={"issue": {"status_id": found[0]["id"], "notes": note}},
                  write=True)

    def assign(self, key, table, current, field, value, note):
        members = (self.call("GET", f"/projects/{quote(current['project'])}/memberships.json",
                             params={"limit": 100}) or {}).get("memberships") or []
        found = {m[k]["id"] for m in members for k in ("user", "group") if _same((m.get(k) or {}).get("name"), value)}
        if len(found) != 1:
            raise TicketToolError(f"the project has {len(found)} members named {value!r}, not one")
        self.call("PUT", f"/issues/{quote(key)}.json", json={"issue": {"assigned_to_id": found.pop(), "notes": note}},
                  write=True)


class Jira(_Client):
    def read(self, key, table):
        fields = (self.call("GET", f"/rest/api/2/issue/{quote(key)}", params={"fields": "status,assignee"}) or {}).get(
            "fields") or {}
        assignee = fields.get("assignee") or {}
        return {"status": _display(fields.get("status")), "assignee": str(assignee.get("displayName") or
                                                                            assignee.get("name") or "")}

    def comment(self, key, note):
        self.call("POST", f"/rest/api/2/issue/{quote(key)}/comment", json={"body": note}, write=True)

    def set_status(self, key, table, current, status, note):
        moves = (self.call("GET", f"/rest/api/2/issue/{quote(key)}/transitions") or {}).get("transitions") or []
        found = [t for t in moves if _same((t.get("to") or {}).get("name"), status)]
        if len(found) != 1:
            raise TicketToolError(f"{key} has {len(found)} transitions to {status!r} from {current['status']!r}, not one")
        self.call("POST", f"/rest/api/2/issue/{quote(key)}/transitions", json={"transition": {"id": found[0]["id"]}},
                  write=True)
        self.comment(key, note)

    def assign(self, key, table, current, field, value, note):
        users = self.call("GET", "/rest/api/2/user/search", params={"username": value}) or []
        found = [u for u in users if _same(u.get("displayName"), value) or _same(u.get("name"), value)]
        if len(found) != 1:
            raise TicketToolError(f"JIRA has {len(found)} users named {value!r}, not one")
        self.call("PUT", f"/rest/api/2/issue/{quote(key)}/assignee", json={"name": found[0]["name"]}, write=True)
        self.comment(key, note)


class ServiceNow(_Client):
    def record(self, key, table):
        rows = (self.call("GET", f"/api/now/table/{table}", params={
            "sysparm_query": f"number={key}", "sysparm_fields": "sys_id,state,assigned_to,assignment_group",
            "sysparm_display_value": "true", "sysparm_limit": 2}) or {}).get("result") or []
        if len(rows) != 1:
            raise TicketToolError(f"ServiceNow {table} has {len(rows)} records numbered {key}, not one")
        return rows[0]

    def read(self, key, table):
        row = self.record(key, table)
        return {"status": _display(row.get("state")), "assignee": _display(row.get("assigned_to")),
                "assignment_group": _display(row.get("assignment_group")), "sys_id": _display(row.get("sys_id"))}

    def patch(self, table, current, body):
        self.call("PATCH", f"/api/now/table/{table}/{quote(current['sys_id'])}",
                  params={"sysparm_input_display_value": "true"}, json=body, write=True)

    def set_status(self, key, table, current, status, note):
        self.patch(table, current, {"state": status, "work_notes": note})

    def assign(self, key, table, current, field, value, note):
        self.patch(table, current, {"assigned_to" if field == "assignee" else field: value, "work_notes": note})


CLIENTS = {"redmine": Redmine, "jira": Jira, "servicenow": ServiceNow}


def _checked(payload: Any, action_class: str) -> dict[str, Any]:
    data = validate_payload(payload, PAYLOAD_SCHEMAS[action_class])
    system = data["system"]
    if not KEY_PATTERNS[system].fullmatch(data["ticket"]):
        raise ToolError(f"{data['ticket']!r} is not a {system} ticket key")
    if (system == "servicenow") != ("table" in data):
        raise ToolError("a ServiceNow ticket needs its table (incident or sc_request); the other systems take none")
    if data.get("field") == "assignment_group" and system != "servicenow":
        raise ToolError("assignment_group is a ServiceNow field")
    for key in ("status", "value"):
        if key in data and any(ord(ch) < 32 or ch == "\x7f" for ch in data[key]):
            raise ToolError(f"{key} may not hold control characters")
    return data


def _act(action_class: str, payload: Any, context: Optional[Mapping[str, Any]], session: Any,
         env: Optional[Mapping[str, str]], verify: Optional[Verifier], field_of: Callable[[dict], str],
         target_of: Callable[[dict], str], expected_of: Callable[[dict], str], write: Callable[..., None]
         ) -> dict[str, Any]:
    data = _checked(payload, action_class)
    approval = require_approval(context, verify, payload=data, action_class=action_class)
    client = CLIENTS[data["system"]](data["endpoint"], os.environ if env is None else env, session)
    key, table, field, target = data["ticket"], data.get("table"), field_of(data), target_of(data)
    label = f"{data['system']} {key}"
    try:
        current = client.read(key, table)
    except TicketToolError as exc:
        return outcome("failed", f"could not read {label} ({exc}); nothing changed")
    if _same(current.get(field), target):
        return outcome("succeeded", f"{label}: {field} is already {target}; nothing changed")
    if not _same(current.get(field), expected_of(data)):
        return outcome("failed", f"{label}: {field} is {current.get(field)!r}, not {expected_of(data)!r} as when it "
                                 "was proposed; the proposal is stale, nothing changed")
    note = (f"{data['note']}\n\nApproved by {approval.decided_by} in escalation {approval.escalation_id} "
            f"(proposal {approval.proposal_id}); applied by the archi ticket-triage automation.")
    try:
        write(client, data, current, note)
    except WriteUnknown as exc:
        return outcome("unknown", f"{label}: the write got no answer ({exc}); it may have applied, check the ticket")
    except TicketToolError as exc:
        return outcome("failed", f"{label}: {exc}")
    try:
        after = client.read(key, table)
    except TicketToolError as exc:
        return outcome("unknown", f"{label}: written, but the check read failed ({exc}); check the ticket")
    if not _same(after.get(field), target):
        return outcome("failed", f"{label}: written, but {field} reads {after.get(field)!r}, not {target!r}")
    return outcome("succeeded", f"{label}: {field} {current.get(field) or '(empty)'!s} -> {target}; noted on the "
                                f"ticket with escalation {approval.escalation_id}")


def set_ticket_status(*, payload: Mapping[str, Any], context: Mapping[str, Any], session: Any = None,
                      env: Optional[Mapping[str, str]] = None, verify: Optional[Verifier] = None) -> dict[str, Any]:
    """Move one ticket to `status` after an approved escalation (see the module docstring)."""
    return _act("set_ticket_status", payload, context, session, env, verify,
                field_of=lambda d: "status", target_of=lambda d: d["status"],
                expected_of=lambda d: d["expected_status"],
                write=lambda c, d, cur, note: c.set_status(d["ticket"], d.get("table"), cur, d["status"], note))


def assign_ticket(*, payload: Mapping[str, Any], context: Mapping[str, Any], session: Any = None,
                  env: Optional[Mapping[str, str]] = None, verify: Optional[Verifier] = None) -> dict[str, Any]:
    """Set one ticket's assignee (ServiceNow: assignment group) after an approved escalation."""
    return _act("assign_ticket", payload, context, session, env, verify,
                field_of=lambda d: d["field"], target_of=lambda d: d["value"], expected_of=lambda d: d["expected"],
                write=lambda c, d, cur, note: c.assign(d["ticket"], d.get("table"), cur, d["field"], d["value"], note))
