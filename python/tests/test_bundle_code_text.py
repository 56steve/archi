"""The optional code-text sources store function bodies (okg#3982).

`code_structure.yaml.example` gives a function's name, signature, docstring
and line numbers, never its body, so "what does this function do when the
name contains a slash" could not be answered from the graph. okg's
`DocCorpusSource` with `PythonSymbolChunker` stores the code itself, one chunk
per function or class. These tests hold the bundle wiring for it:

- the install keeps `${deployment_dir}` for okg to expand at run time, and
  installs the extractors.yaml that the source reads from there;
- the shipped parameters, run over a synthetic repository, store a function's
  body with its line range and name;
- the shipped excludes keep credential-named code files out (a control run
  without them stores those files);
- every matched extension reaches a parser, and the GitLab twin differs from
  the GitHub file only in its names.

Synthetic fixtures only; no real repository content.
"""

import inspect
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

BUNDLE = Path(__file__).resolve().parents[2] / "bundles" / "cern-team"
SOURCE_DEFAULTS = BUNDLE / "source-defaults"
EXAMPLES = {
    "code_text": SOURCE_DEFAULTS / "code_text.yaml.example",
    "code_text_gitlab": SOURCE_DEFAULTS / "code_text_gitlab.yaml.example",
}

#: Only this function's body contains the marker, not its name, signature or
#: docstring: finding the marker proves the body was stored.
_MODULE = '''"""Bucket helpers."""

PREFIX = "bkt_"


def bucket_label(name=None):
    """Return the label for a bucket."""
    if not name:
        return PREFIX + "default"
    if "/" in name:
        raise ValueError("zanzibar: a slash is not allowed in a bucket label")
    return PREFIX + name


class Store:
    """A tiny store."""

    def put(self, key, value):
        self.data[key] = value
'''


def _entry(name):
    return yaml.safe_load(EXAMPLES[name].read_text(encoding="utf-8"))[name]


def _plan(tmp_path, monkeypatch, *, enable):
    from okg.substrate.deployment_bootstrap.profile_init import (
        build_plan,
        discover_profile,
    )

    profiles = tmp_path / "profiles"
    shutil.copytree(BUNDLE.parent, profiles, symlinks=False)
    sd = profiles / "cern-team" / "source-defaults"
    for stem in enable:
        (sd / f"{stem}.yaml.example").rename(sd / f"{stem}.yaml")
    monkeypatch.setenv("OKG_PROFILES_DIR", str(profiles))
    return build_plan(
        profile=discover_profile("cern-team"),
        deployment_name="qt",
        deployments_root=tmp_path / "deployments",
        raw_answers={
            "deployment_name": "qt",
            "postgres_dsn": "postgresql://localhost/qt",
            "github_repo_name": "demo",
            "github_repo_url": "https://example.org/demo.git",
            "archi_data_root": str(tmp_path / "data"),
        },
    )


def test_install_keeps_the_runtime_anchor_and_installs_extractors(
    tmp_path, monkeypatch
):
    """`${deployment_dir}` survives the install and okg expands it later.

    The installer refuses any `${name}` it has no answer for, so the source
    cannot name `${deployment_dir}` directly. The `code_extractors_dir` answer
    defaults to that literal text, and the installer does not re-expand an
    answer's value. okg's registry loader then replaces it with the directory
    the registry sits in, which is where `files:` put extractors.yaml.
    """
    from okg.substrate.deployment_bootstrap.profile_init import planned_writes
    from okg.substrate.sources.registry import load_source_registry

    plan = _plan(tmp_path, monkeypatch, enable=["github_repo", "code_text"])
    params = yaml.safe_load(plan.source_defaults["code_text"])["code_text"]["params"]
    assert params["extractors_dir"] == "${deployment_dir}"
    assert params["repo_root"] == str(tmp_path / "data") + "/repos/demo"
    assert params["predecessor_source"] == "qt.github-repo"

    for write in planned_writes(plan):
        write.path.parent.mkdir(parents=True, exist_ok=True)
        if getattr(write, "data", None) is not None:
            write.path.write_bytes(write.data)
        else:
            write.path.write_text(write.text, encoding="utf-8")
    deployment = plan.deployment_dir
    assert (deployment / "extractors.yaml").read_bytes() == (
        BUNDLE / "extractors.yaml"
    ).read_bytes()

    entries = load_source_registry(deployment / "source_registry.yaml")
    loaded = entries["code_text"].raw["params"]
    assert Path(loaded["extractors_dir"]) == deployment.resolve()
    assert (Path(loaded["extractors_dir"]) / "extractors.yaml").is_file()


def test_a_bare_install_still_selects_only_the_default_source(tmp_path, monkeypatch):
    """Shipping extractors.yaml selects no source; the examples stay opt-in."""
    plan = _plan(tmp_path, monkeypatch, enable=[])
    assert "code_text" not in plan.source_defaults
    assert "code_text_gitlab" not in plan.source_defaults


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_the_entry_binds_against_doc_corpus_source(name):
    from okg.substrate.library.sources.doc_corpus import DocCorpusSource

    entry = _entry(name)
    assert entry["module"] == "okg.substrate.library.sources.doc_corpus"
    assert entry["class"] == "DocCorpusSource"
    inspect.signature(DocCorpusSource).bind(**entry["params"])


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_every_matched_extension_reaches_a_parser(name):
    """A matched file with no parser becomes a document that stores nothing."""
    from okg.substrate.library.extraction.chunk.python_symbols import (
        PythonSymbolChunker,
    )
    from okg.substrate.library.extraction.registry import load_extractors
    from okg.substrate.library.sources.doc_corpus import DEFAULT_EXTENSION_TO_FORMAT

    params = _entry(name)["params"]
    routes = dict(DEFAULT_EXTENSION_TO_FORMAT)
    routes.update(params["extension_to_format"])
    bundle = load_extractors(BUNDLE)
    for glob in params["include_globs"]:
        ext = "." + glob.rsplit(".", 1)[1]
        fmt = routes.get(ext)
        assert fmt is not None, f"{ext} is matched but has no format"
        assert fmt in bundle.parsers, (
            f"{ext} routes to {fmt!r}, which extractors.yaml has no parser for"
        )
        assert bundle.chunker_for_format(fmt) is not None
    assert isinstance(bundle.chunker_for_format("code"), PythonSymbolChunker)


def test_the_gitlab_twin_differs_only_in_its_names():
    github = yaml.safe_dump(_entry("code_text"), sort_keys=True)
    gitlab = yaml.safe_dump(_entry("code_text_gitlab"), sort_keys=True)
    renamed = (
        github.replace("code_text", "code_text_gitlab")
        .replace(".code-text", ".code-text-gitlab")
        .replace("github_repo_name", "gitlab_repo_name")
        .replace(".github-repo", ".gitlab-repo")
        .replace("GitHub", "GitLab")
    )
    assert gitlab == renamed


#: Code files whose NAMES mark them as credentials. Only the shipped
#: `exclude_globs` can keep them out: their extension matches `include_globs`.
#: Invented values only.
_CREDENTIAL_CODE = {
    "pkg/secrets/keys.py": 'API_KEY = "not-a-real-key"\n',
    "pkg/aws_credentials.py": 'SECRET = "not-a-real-secret"\n',
}


def _git_repo(root: Path) -> Path:
    repo = root / "repos" / "demo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "buckets.py").write_text(_MODULE, encoding="utf-8")
    for rel, body in _CREDENTIAL_CODE.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    for cmd in (["init", "-q"], ["add", "-A", "-f"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True, env=env)
    return repo


def _run(repo: Path, **overrides):
    """Run okg's reader with the shipped parameters over `repo`.

    `predecessor_source` is dropped because it needs a database; the live
    install proves it (pact evidence).
    """
    from okg.substrate.library.sources.doc_corpus import DocCorpusSource

    params = dict(_entry("code_text")["params"])
    params.pop("predecessor_source")
    params.update(repo="demo", repo_root=str(repo), extractors_dir=str(BUNDLE))
    params.update(overrides)
    run = DocCorpusSource(**params).run("probe", mode="scope_complete")
    return list(run.facts)  # an iterator: read it once


def _documents(facts):
    return sorted(f.node_id for f in facts if getattr(f, "subtype", None) == "document")


def test_the_excludes_keep_credential_named_code_out(tmp_path):
    """The shipped excludes, not the include globs, stop these files.

    Both files end in `.py`, so the include globs match them. The control run
    with no excludes stores them, which proves the shipped list is what keeps
    them out.
    """
    repo = _git_repo(tmp_path)
    credential_docs = sorted(f"doc:demo:{rel}" for rel in _CREDENTIAL_CODE)

    assert _documents(_run(repo, exclude_globs=[])) == sorted(
        ["doc:demo:pkg/buckets.py", *credential_docs]
    )
    shipped = _run(repo)
    assert _documents(shipped) == ["doc:demo:pkg/buckets.py"]
    stored = "\n".join(
        str(f.attrs.get("text", ""))
        for f in shipped
        if getattr(f, "subtype", None) == "document_chunk"
    )
    assert "not-a-real" not in stored


def test_the_shipped_parameters_store_a_function_body(tmp_path):
    """End to end over okg's reader: a body-only word comes back with its lines."""
    facts = _run(_git_repo(tmp_path))
    chunks = [f for f in facts if getattr(f, "subtype", None) == "document_chunk"]
    body = [c for c in chunks if "zanzibar" in str(c.attrs.get("text", ""))]
    assert len(body) == 1, "the function body was not stored as one chunk"
    attrs = body[0].attrs
    text = attrs["text"]
    assert "def bucket_label" in text
    assert 'raise ValueError("zanzibar' in text
    assert "class Store" not in text, "the chunk is not cut at the function"
    lines = _MODULE.splitlines()
    start = int(attrs["start_line"])
    end = int(attrs["end_line"])
    # The range may start at the blank lines above the `def` (okg#3982 item 5);
    # it must still cover the whole function.
    assert "def bucket_label" in "\n".join(lines[start - 1:end])
    assert "zanzibar" in "\n".join(lines[start - 1:end])
    assert attrs["symbol"] == "bucket_label"
    methods = [c for c in chunks if c.attrs.get("symbol") == "Store"]
    assert methods and "self.data[key] = value" in methods[0].attrs["text"]
