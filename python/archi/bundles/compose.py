"""Merge the selected packs into one profile's worth of content.

Each rule is ADR 0002 section 3.3's table, applied in resolution order:

| part | rule |
|---|---|
| modules | set union |
| schemas | union by relative path; one path, two contents: refused with both digests |
| sources | ids become `<pack>.<source>` |
| init questions | union; a pack's own ids become `<pack>_<id>` (flag `--<pack>-<id>`); one id, two definitions: refused |
| invariants, freshness | union; each stays with its pack; one name, two bodies: refused |
| search `content` | subtype union |
| other search profiles | union by name; same name, different body: stop |
| playbooks, triggers | union; one file name (or intent id), two contents: refused |
| action classes | union by id; one id, two bodies: refused |
| files | union by path; one path, two contents: refused |
| chat prompt | base prompt, then each pack's fragment in resolution order |

`compose` touches only the packs in the resolution, so a pack that is not
selected contributes nothing.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from archi.bundles.manifest import BASE_DIR, TEMPLATE_REF, Pack, base_questions
from archi.bundles.resolve import CompositionError, Resolution

#: Rendered skills/ entries the renderer writes itself.
PROMPT_FILE = "chat-system-prompt.md"
TRIGGERS_FILE = "skill-triggers.yaml"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def question_prefix(pack: Pack) -> str:
    return pack.name.replace("-", "_") + "_"


def source_id(pack: Pack, source: str) -> str:
    return f"{pack.name}.{source}"


@dataclass
class Composition:
    """Everything a rendered profile holds, with the pack each part came from."""

    modules: list[str] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    #: rendered source id -> template text (the id is the file stem too)
    sources: dict[str, str] = field(default_factory=dict)
    schemas: dict[str, bytes] = field(default_factory=dict)
    invariants: list[dict[str, Any]] = field(default_factory=list)
    playbooks: dict[str, bytes] = field(default_factory=dict)
    triggers: Optional[dict[str, Any]] = None
    action_classes: dict[str, Any] = field(default_factory=dict)
    freshness: dict[str, dict[str, str]] = field(default_factory=dict)
    search_subtypes: list[str] = field(default_factory=list)
    #: named okg search profiles other than `content`
    search_profiles: dict[str, Any] = field(default_factory=dict)
    live_tools: list[str] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    prompt: str = ""
    #: "<kind>:<key>" -> packs that contributed it (several when identical)
    owners: dict[str, list[str]] = field(default_factory=dict)

    def own(self, kind: str, key: str, pack: str) -> None:
        owners = self.owners.setdefault(f"{kind}:{key}", [])
        if pack not in owners:
            owners.append(pack)


def _files_under(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _merge_bytes(
    comp: Composition, target: dict[str, bytes], kind: str, rel: str, data: bytes, pack: Pack
) -> None:
    existing = target.get(rel)
    if existing is not None and existing != data:
        first = comp.owners[f"{kind}:{rel}"][0]
        raise CompositionError(
            f"{kind}_conflict",
            f"{kind} {rel!r} differs between packs {first!r} ({digest(existing)}) "
            f"and {pack.name!r} ({digest(data)}); packs shipping the same path must "
            "ship identical bytes",
            path=rel,
            packs=[first, pack.name],
            digests=[digest(existing), digest(data)],
        )
    target[rel] = data
    comp.own(kind, rel, pack.name)


def _merge_value(
    comp: Composition, target: dict[str, Any], kind: str, key: str, value: Any, pack: Pack
) -> None:
    if key in target and target[key] != value:
        first = comp.owners[f"{kind}:{key}"][0]
        raise CompositionError(
            f"{kind}_conflict",
            f"{kind} {key!r} is defined differently by packs {first!r} and {pack.name!r}; "
            "the same id must have an identical definition",
            key=key,
            packs=[first, pack.name],
        )
    target[key] = value
    comp.own(kind, key, pack.name)


def render_template(pack: Pack, source: str, text: str) -> str:
    """Rename the template's top-level key and namespace its question refs."""
    local = {q["id"] for q in pack.questions()}
    prefix = question_prefix(pack)

    def rename(match: re.Match[str]) -> str:
        name = match.group(1)
        return "${" + (prefix + name if name in local else name) + "}"

    text = TEMPLATE_REF.sub(rename, text)
    renamed, count = re.subn(
        rf"^{re.escape(source)}:", f"{source_id(pack, source)}:", text, count=1, flags=re.M
    )
    if count != 1:  # validate_pack guarantees the key; keep the failure loud
        raise CompositionError(
            "source_template_key", f"{pack.dir}: template {source!r} has no top-level {source}: key"
        )
    return renamed


def base_prompt() -> str:
    return (BASE_DIR / PROMPT_FILE).read_text(encoding="utf-8")


def compose(resolution: Resolution) -> Composition:
    comp = Composition()
    question_defs: dict[str, dict[str, Any]] = {}

    for q in base_questions():
        q = dict(q)
        if q["id"] == "chat_site_name" and "site_name" in resolution.chat:
            q["default"] = resolution.chat["site_name"]
        if q["id"] == "chat_model" and "default_model" in resolution.chat:
            q["default"] = resolution.chat["default_model"]
        question_defs[q["id"]] = q
        comp.own("question", q["id"], "(base)")

    fragments: list[str] = []
    trigger_version: Optional[Any] = None
    intents: dict[str, Any] = {}

    for pack in resolution.packs:
        for module in pack.requires_modules:
            if module not in comp.modules:
                comp.modules.append(module)

        if pack.path("schemas") is not None:
            for rel, data in _files_under(pack.path("schemas")).items():
                _merge_bytes(comp, comp.schemas, "schema", rel, data, pack)

        for source, template in pack.sources().items():
            sid = source_id(pack, source)
            comp.sources[sid] = render_template(pack, source, template.read_text(encoding="utf-8"))
            comp.own("source", sid, pack.name)

        for q in pack.questions():
            q = dict(q)
            q["id"] = question_prefix(pack) + q["id"]
            _merge_value(comp, question_defs, "question", q["id"], q, pack)

        if pack.path("invariants") is not None:
            body = yaml.safe_load(pack.path("invariants").read_text(encoding="utf-8"))
            invariants: dict[str, Any] = {i["name"]: i for i in comp.invariants}
            for inv in body.get("invariants", []):
                _merge_value(comp, invariants, "invariant", inv["name"], inv, pack)
            comp.invariants = list(invariants.values())

        for sid, slo in pack.freshness.items():
            comp.freshness[sid] = dict(slo)
            comp.own("freshness", sid, pack.name)

        for subtype in pack.search_subtypes:
            if subtype not in comp.search_subtypes:
                comp.search_subtypes.append(subtype)
            comp.own("search_subtype", subtype, pack.name)

        for name, body in pack.manifest.get("search_profiles", {}).items():
            _merge_value(comp, comp.search_profiles, "search_profile", name, dict(body), pack)

        for tool in pack.live_tools:
            if tool not in comp.live_tools:
                comp.live_tools.append(tool)
            comp.own("live_tool", tool, pack.name)

        triggers_path = pack.path("skill_triggers")
        if pack.path("playbooks") is not None:
            for rel, data in _files_under(pack.path("playbooks")).items():
                if triggers_path is not None and pack.path("playbooks") / rel == triggers_path:
                    continue
                if rel in (PROMPT_FILE, TRIGGERS_FILE):
                    raise CompositionError(
                        "playbook_reserved",
                        f"pack {pack.name!r} ships playbook {rel!r}; the renderer writes that "
                        "file itself (the chat prompt is built from prompt_fragment)",
                        pack=pack.name,
                        path=rel,
                    )
                _merge_bytes(comp, comp.playbooks, "playbook", rel, data, pack)

        if triggers_path is not None:
            body = yaml.safe_load(triggers_path.read_text(encoding="utf-8"))
            version = body.get("version")
            if trigger_version is not None and version != trigger_version:
                raise CompositionError(
                    "trigger_version_conflict",
                    f"pack {pack.name!r} ships skill triggers version {version!r}; "
                    f"earlier packs use {trigger_version!r}",
                    pack=pack.name,
                )
            trigger_version = version
            for intent, value in body["intents"].items():
                _merge_value(comp, intents, "trigger", intent, value, pack)

        if pack.path("action_classes") is not None:
            body = yaml.safe_load(pack.path("action_classes").read_text(encoding="utf-8"))
            for aid, value in body["action_classes"].items():
                _merge_value(comp, comp.action_classes, "action_class", aid, value, pack)

        for rel in pack.files:
            _merge_bytes(comp, comp.files, "file", rel, (pack.dir / rel).read_bytes(), pack)

        if pack.path("prompt_fragment") is not None:
            fragments.append(pack.path("prompt_fragment").read_text(encoding="utf-8").strip())

    comp.questions = list(question_defs.values())
    if intents:
        comp.triggers = {"version": trigger_version, "intents": intents}
    comp.prompt = "\n\n".join([base_prompt().rstrip()] + fragments) + "\n"
    return comp


def deployment_defaults(resolution: Resolution, comp: Composition, profile_name: str) -> dict:
    """The base deployment defaults with the pack- and bundle-decided parts filled."""
    data = yaml.safe_load((BASE_DIR / "deployment-defaults.yaml").read_text(encoding="utf-8"))
    data = copy.deepcopy(data)
    data["nomos"]["rollout"]["owner"]["name"] = f"archi-{profile_name}"
    if not comp.search_subtypes:
        raise CompositionError(
            "search_subtypes_empty",
            "no selected pack declares search_subtypes, so the `content` search "
            "profile would be empty; select a context pack that declares them",
        )
    data["search"]["profiles"]["content"]["subtypes"] = list(comp.search_subtypes)
    for name, body in comp.search_profiles.items():
        data["search"]["profiles"][name] = dict(body)
    preset = data["chat"]["preset"]
    if "description" in resolution.chat:
        preset["description"] = resolution.chat["description"]
    if resolution.chat.get("suggestions"):
        preset["suggestions"] = list(resolution.chat["suggestions"])
    else:
        preset.pop("suggestions")
    if comp.triggers is None:
        data.pop("skill_triggers")
    return data
