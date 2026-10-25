"""Checks for automation packs (ADR 0002 sections 7.2 and 7.3, PACT r3-ops-runtime).

`validate_pack` calls `automation_problems` for a pack of kind `automation`
once the JSON schema has passed. The schema fixes the shape; this module
checks what it cannot see: that the playbook exists, that every action class
the automation names is defined once, that each mediator resolves, that every
JSON schema is a valid JSON schema, that the context gate is a real
`GateSpec`, that the budget is positive and that no level is above L2.

The manifest shape:

    automation:
      trigger: {type: ticket_queue, system: redmine, queue: cms-comp-ops}
      context_gate: {sources: [...], max_staleness: 24h, invariants: [...]}
      playbook: triage.md              # under the pack's `playbooks` directory
      action_classes: [draft_reply]    # defined in the `action_classes` file
      proposal_schema: {...}           # JSON Schema; requires the ops_proposal fields
      budget: {cost_per_run: 0.5, cost_per_day: 10, runs_per_hour: 20}
      default_level: L1
      max_level: L2
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from archi.bundles.manifest import PACK_FILE, Problem, _schema_problems
from archi.ops.context_gate import GateSpec, GateSpecError

#: Autonomy levels the 3.0 runtime accepts, lowest first (ADR 0002 section 7.3).
LEVELS = ("L0", "L1", "L2")
D13 = "ADR 0002 D13: 3.0 stops at L2, L3 is reserved and refused"

#: Workflow steps a mediator may name instead of a live tool. The loop
#: (PACT r3-ops-runtime, task loop) implements them.
WORKFLOW_STEPS = ("record_finding", "draft_reply", "update_ticket")

#: The ops_proposal fields an automation's proposal carries
#: (bundles/packs/ops-record/schemas/ops_record.yaml). The runtime adds the
#: rest (incident_id, mode, autonomy_level, generation_read, ...).
PROPOSAL_FIELDS = ("summary", "evidence_ids", "action_class", "payload", "confidence", "rollback")

ID = re.compile(r"^[a-z][a-z0-9_]*$")
TOOL_ENTRY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
CRON_FIELD = re.compile(r"^[0-9A-Za-z*/,\-?]+$")
SYSTEMS = ("redmine", "jira", "servicenow")
TRIGGER_KEYS = {
    "schedule": ("cron",),
    "ticket_queue": ("system", "queue"),
    "signal": ("source", "condition"),
}


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that keeps a repeated mapping key instead of dropping it."""


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def level_problem(level: Any, where: str) -> str | None:
    """Why `level` is refused, or None when it is L0, L1 or L2."""
    if level in LEVELS:
        return None
    if level == "L3":
        return f"{where}: L3 is refused ({D13})"
    return f"{where}: unknown level {level!r} is refused; use one of {LEVELS} ({D13})"


def _rank(level: str) -> int:
    return LEVELS.index(level)


def _schema_check(schema: Any, where: str) -> list[str]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"{where}: not a valid JSON Schema: {exc.message}"]
    if not isinstance(schema, Mapping) or schema.get("type") != "object":
        return [f"{where}: must be an object schema (`type: object`)"]
    return []


def _trigger_messages(trigger: Mapping[str, Any]) -> list[str]:
    kind = trigger["type"]
    out = []
    allowed = TRIGGER_KEYS[kind]
    for key in allowed:
        if key not in trigger:
            out.append(f"automation.trigger: a {kind} trigger needs `{key}`")
    for key in sorted(set(trigger) - {"type"} - set(allowed)):
        out.append(f"automation.trigger: `{key}` does not belong to a {kind} trigger (allowed: {', '.join(allowed)})")
    if kind == "schedule" and "cron" in trigger:
        fields = trigger["cron"].split()
        if len(fields) != 5 or not all(CRON_FIELD.match(f) for f in fields):
            out.append(f"automation.trigger.cron: {trigger['cron']!r} is not a five-field cron expression")
    if kind == "ticket_queue" and trigger.get("system") not in (None, *SYSTEMS):
        out.append(f"automation.trigger.system: {trigger['system']!r} is not one of {SYSTEMS}")
    return out


def _sibling_pack(directory: Path, name: str) -> Mapping[str, Any] | None:
    path = directory.parent / name / PACK_FILE
    try:
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return body if isinstance(body, Mapping) else None


def _action_class_messages(directory: Path, data: Mapping[str, Any], names: list[str]) -> tuple[list[str], dict]:
    """Messages for the `action_classes` file, and the classes it defines."""
    rel = data.get("action_classes")
    if rel is None:
        return ["action_classes: an automation pack must declare an `action_classes:` file"], {}
    path = directory / rel
    try:
        body = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        reason = getattr(exc, "problem", None) or str(exc)
        return [f"{rel}: action class ids must be unique: {reason}"], {}
    classes = body.get("action_classes") if isinstance(body, Mapping) else None
    if not isinstance(classes, Mapping):
        return [], {}  # validate_pack already reports the shape

    own_tools = set(data.get("live_tools", ()))
    required = list(data.get("requires", {}).get("packs", ()))
    tools = set(own_tools)
    unreadable = []
    for dep in required:
        dep_manifest = _sibling_pack(directory, dep)
        if dep_manifest is None:
            unreadable.append(dep)
        else:
            tools |= set(dep_manifest.get("live_tools", ()))

    out = []
    for cid, cls in classes.items():
        where = f"{rel}: action class {cid!r}"
        if not isinstance(cid, str) or not ID.match(cid):
            out.append(f"{rel}: action class id {cid!r} must match {ID.pattern}")
        problems = _schema_problems("action_class", cls, path)
        if problems:
            out.extend(f"{where}: {p.message}" for p in problems)
            continue
        out.extend(f"{where}: {m}" for m in _schema_check(cls["payload_schema"], "payload_schema"))
        msg = level_problem(cls["max_level"], f"{where}: max_level")
        if msg:
            out.append(msg)
        mediator = cls["mediator"]
        if TOOL_ENTRY.match(mediator):
            if mediator not in tools:
                hint = f" (could not read required pack(s) {unreadable})" if unreadable else ""
                out.append(
                    f"{where}: mediator {mediator!r} is not a live tool of this pack or of a pack it requires{hint}"
                )
        elif mediator not in WORKFLOW_STEPS:
            out.append(
                f"{where}: mediator {mediator!r} is neither a live tool entry (archi.tools.x:fn) "
                f"nor a known workflow step ({', '.join(WORKFLOW_STEPS)})"
            )
    for name in names:
        if name not in classes:
            out.append(f"automation.action_classes: {name!r} is not defined in {rel}")
    return out, dict(classes)


def automation_problems(directory: Path, data: Mapping[str, Any]) -> list[Problem]:
    """Problems with the automation a (schema-valid) pack manifest declares."""
    manifest = directory / PACK_FILE
    if data["kind"] != "automation":
        if "automation" in data:
            return [Problem(manifest, "automation: only a pack of kind `automation` may declare an automation")]
        return []
    if "automation" not in data:
        return [Problem(manifest, "an automation pack must declare an `automation:` block")]

    auto = data["automation"]
    messages: list[str] = []

    messages.extend(_trigger_messages(auto["trigger"]))

    try:
        gate = GateSpec.from_mapping(auto["context_gate"])
    except GateSpecError as exc:
        messages.append(f"automation.context_gate: {exc}")
    else:
        if not (gate.sources or gate.invariants or gate.min_context_eval_score is not None):
            messages.append(
                "automation.context_gate: declares no check; name at least one source, invariant or min_context_eval_score (ADR 0002 D12)"
            )

    playbooks = data.get("playbooks")
    if playbooks is None:
        messages.append("playbooks: an automation pack must declare a `playbooks:` directory")
    elif not (directory / playbooks / auto["playbook"]).is_file():
        messages.append(f"automation.playbook: {auto['playbook']!r} is not a file under {playbooks!r}")

    names = auto["action_classes"]
    for name in sorted({n for n in names if names.count(n) > 1}):
        messages.append(f"automation.action_classes: {name!r} is listed more than once")
    class_messages, classes = _action_class_messages(directory, data, list(dict.fromkeys(names)))
    messages.extend(class_messages)

    proposal = auto["proposal_schema"]
    schema_msgs = _schema_check(proposal, "automation.proposal_schema")
    messages.extend(schema_msgs)
    if not schema_msgs:
        props = proposal.get("properties", {})
        for field in sorted(set(props) - set(PROPOSAL_FIELDS)):
            messages.append(
                f"automation.proposal_schema: property {field!r} is not an ops_proposal field ({', '.join(PROPOSAL_FIELDS)})"
            )
        missing = [f for f in PROPOSAL_FIELDS if f not in proposal.get("required", ())]
        if missing:
            messages.append(f"automation.proposal_schema: `required` must list {', '.join(missing)}")

    budget = auto["budget"]
    for key in ("cost_per_run", "cost_per_day", "runs_per_hour"):
        value = budget[key]
        if isinstance(value, bool) or value <= 0:
            messages.append(f"automation.budget.{key}: must be greater than zero, got {value!r}")
    if all(not isinstance(budget[k], bool) and budget[k] > 0 for k in ("cost_per_run", "cost_per_day")):
        if budget["cost_per_run"] > budget["cost_per_day"]:
            messages.append("automation.budget: cost_per_run exceeds cost_per_day")

    levels_ok = True
    for key in ("default_level", "max_level"):
        msg = level_problem(auto[key], f"automation.{key}")
        if msg:
            messages.append(msg)
            levels_ok = False
    if levels_ok and _rank(auto["default_level"]) > _rank(auto["max_level"]):
        messages.append(
            f"automation: default_level {auto['default_level']} is above max_level {auto['max_level']}"
        )

    return [Problem(manifest, m) for m in messages]
