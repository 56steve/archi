"""Helpers shared by the pack/bundle tests (imported by module name)."""
from pathlib import Path

import yaml

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATALOG = FIXTURES / "library"


def edit_yaml(path: Path, change) -> None:
    """Load `path`, let `change(data)` mutate it, write it back."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# cern-team, expressed as packs
# ---------------------------------------------------------------------------
#
# The real cern-team profile (bundles/cern-team/), regrouped at test time into
# one pack per source family plus a playbook pack. Nothing in the repository
# moves: splitting the shipped bundle is PACT r3-packs-split. This is the
# renderer's real input: every template, schema slice, invariant, playbook and
# question below is read from the shipped files.

import re
import shutil

REPO = Path(__file__).resolve().parents[3]
CERN_TEAM = REPO / "bundles" / "cern-team"

#: pack -> (source-default files, needs_credentials, extra modules)
CERN_TEAM_PACKS = {
    "cmssw-releases": (["cmssw_releases.yaml"], False, []),
    "docsite": (["docsite.yaml.example"], False, []),
    "indico": (["indico.yaml.example", "indico_live.yaml.example"], False, []),
    "jira": (["jira.yaml.example"], False, []),
    "cds": (["cds.yaml.example"], False, []),
    "twiki": (["twiki_eos.yaml.example"], False, []),
    "github": (
        ["github_repo.yaml.example", "git_history.yaml.example",
         "code_text.yaml.example", "code_structure.yaml.example"],
        False, ["git_graph"],
    ),
    "gitlab": (
        ["gitlab_repo.yaml.example", "code_text_gitlab.yaml.example",
         "code_structure_gitlab.yaml.example"],
        False, ["git_graph"],
    ),
}
CERN_TEAM_DEFAULT = ["cmssw-releases", "cern-playbooks"]
BASE_QUESTION_IDS = {
    "deployment_name", "postgres_dsn", "archi_data_root", "chat_site_name", "chat_model",
}
REF = re.compile(r"\$\{([a-z_]+)\}")


def _profile():
    return yaml.safe_load((CERN_TEAM / "profile.yaml").read_text(encoding="utf-8"))


def cern_team_question(qid):
    return next(q for q in _profile()["init_questions"] if q["id"] == qid)


def _pack_manifest(name, **extra):
    data = {
        "name": name,
        "kind": "context",
        "version": "0.1.0",
        "description": f"cern-team {name} sources (test regrouping).",
    }
    data.update(extra)
    return data


def build_cern_team_catalog(dest: Path) -> Path:
    """Write cern-team as packs + one bundle under `dest`; return `dest`."""
    modules = yaml.safe_load((CERN_TEAM / "modules.yaml").read_text())["modules"]
    base_modules = [m for m in modules if m != "git_graph"]
    packs = dest / "packs"
    for name, (files, needs_credentials, extra_modules) in CERN_TEAM_PACKS.items():
        pdir = packs / name
        (pdir / "sources").mkdir(parents=True)
        shutil.copytree(CERN_TEAM / "schemas", pdir / "schemas")
        refs, subtypes = set(), []
        for filename in files:
            text = (CERN_TEAM / "source-defaults" / filename).read_text(encoding="utf-8")
            stem = filename.split(".")[0]
            (pdir / "sources" / f"{stem}.yaml").write_text(text, encoding="utf-8")
            refs |= set(REF.findall(text))
            body = yaml.safe_load(text)[stem]
            for node in body.get("admission_policy", {}).get("output_signature", {}).get("nodes", []):
                if node["subtype"] not in subtypes:
                    subtypes.append(node["subtype"])
        manifest = _pack_manifest(
            name,
            requires={"modules": base_modules + extra_modules},
            schemas="schemas/",
            sources="sources/",
            search_subtypes=subtypes or ["document_chunk"],
            needs_credentials=needs_credentials,
        )
        local = sorted(refs - BASE_QUESTION_IDS)
        if local:
            (pdir / "questions.yaml").write_text(
                yaml.safe_dump({"init_questions": [cern_team_question(q) for q in local]}, sort_keys=False)
            )
            manifest["init_questions"] = "questions.yaml"
        if name == "cmssw-releases":
            shutil.copy(CERN_TEAM / "invariants.yaml", pdir / "invariants.yaml")
            manifest["invariants"] = "invariants.yaml"
        if name in ("github", "gitlab"):
            shutil.copy(CERN_TEAM / "extractors.yaml", pdir / "extractors.yaml")
            manifest["files"] = ["extractors.yaml"]
        (pdir / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    pdir = packs / "cern-playbooks"
    (pdir / "playbooks").mkdir(parents=True)
    for path in sorted((CERN_TEAM / "skills").iterdir()):
        if path.name == "chat-system-prompt.md":
            continue  # the base prompt; the renderer writes it
        shutil.copyfile(path, pdir / "playbooks" / path.name)  # follows the symlink
    manifest = _pack_manifest(
        "cern-playbooks",
        kind="tool",
        playbooks="playbooks/",
        skill_triggers="playbooks/skill-triggers.yaml",
        needs_credentials=False,
    )
    manifest["description"] = "The archi playbooks cern-team ships (test regrouping)."
    (pdir / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    bdir = dest / "cern-team"
    bdir.mkdir(parents=True)
    profile = _profile()
    questions = {q["id"]: q for q in profile["init_questions"]}
    optional = [p for p in CERN_TEAM_PACKS if p not in CERN_TEAM_DEFAULT]
    (bdir / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "cern-team",
                "version": profile["version"],
                "description": "cern-team, regrouped into packs for the renderer tests.",
                "packs": {"default": CERN_TEAM_DEFAULT, "optional": optional},
                "chat": {
                    "site_name": questions["chat_site_name"]["default"],
                    "default_model": questions["chat_model"]["default"],
                },
                "okg": ">=0.2.0,<0.3",
            },
            sort_keys=False,
        )
    )
    return dest


def clone_pack(root: Path, source: str, name: str, change=None) -> Path:
    """Copy pack `source` under `root/packs/` as `name`, renaming everything
    that carries the pack name, then let `change(manifest)` edit it."""
    src = root / "packs" / source
    dst = root / "packs" / name
    shutil.copytree(src, dst)
    data = yaml.safe_load((dst / "pack.yaml").read_text())
    data["name"] = name
    if "freshness" in data:
        data["freshness"] = {
            f"{name}.{k.split('.', 1)[1]}": v for k, v in data["freshness"].items()
        }
    if change is not None:
        change(data)
    (dst / "pack.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return dst


def write_bundle(root: Path, name: str, **fields) -> Path:
    data = {"name": name, "version": "0.1.0", "description": f"{name} test bundle."}
    data.update(fields)
    data.setdefault("okg", ">=0.2.0,<0.3")
    bdir = root / name
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "bundle.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return bdir


def load_catalog(root: Path):
    from archi.bundles import discovery

    catalog = discovery.from_roots([root / "packs"], [root])
    assert catalog.problems == [], [str(p) for p in catalog.problems]
    return catalog
