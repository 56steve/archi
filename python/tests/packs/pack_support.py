"""Helpers shared by the pack tests (imported by module name).

`FIXTURES` gives every shipped context pack the inputs it reads, so the pack
can be installed alone and publish (acc.r3-packs-split.matrix). A fixture is
a function `(data_root, server) -> answers`: it writes the caches the pack's
sources read under `data_root`, registers any HTTP responses a live source
fetches on `server`, and returns the pack's own init-question answers by
their local ids.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

import yaml

REPO = Path(__file__).resolve().parents[3]
PACKS_DIR = REPO / "bundles" / "packs"
BUNDLES_DIR = REPO / "bundles"
SKILLS_DIR = REPO / "skills"
TEST_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def shipped_catalog():
    """The packs and bundles this repository ships, with no problems."""
    from archi.bundles import discovery

    catalog = discovery.from_roots([PACKS_DIR], [BUNDLES_DIR])
    assert catalog.problems == [], [str(p) for p in catalog.problems]
    return catalog


def pack_dirs() -> list[Path]:
    return sorted(p.parent for p in PACKS_DIR.glob("*/pack.yaml"))


def manifest(pack: str) -> dict:
    return yaml.safe_load((PACKS_DIR / pack / "pack.yaml").read_text(encoding="utf-8"))


def context_packs() -> list[str]:
    return [p.name for p in pack_dirs() if manifest(p.name)["kind"] == "context"]


def templates(pack: str) -> dict[str, dict]:
    """source name -> registry entry, for one pack."""
    out = {}
    for path in sorted((PACKS_DIR / pack / "sources").glob("*.yaml")):
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
        out[path.stem] = body[path.stem]
    return out


def all_templates() -> list[tuple[str, str, Path, dict]]:
    """(pack, source, file, entry) for every source every pack ships."""
    out = []
    for pack in context_packs():
        for source, entry in templates(pack).items():
            out.append((pack, source, PACKS_DIR / pack / "sources" / f"{source}.yaml", entry))
    return out


def invariants(pack: str) -> list[dict]:
    path = PACKS_DIR / pack / "invariants.yaml"
    if not path.is_file():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8"))["invariants"]


_REQUIRED = re.compile(r"required\(subtype\)\s+AS\s+\(\s*VALUES\s+(.*?)\n\s*\)\s*,", re.S)


def required_subtypes(invariant: dict) -> list[str]:
    """The subtypes a coverage invariant requires (its `required` VALUES list)."""
    match = _REQUIRED.search(invariant["sql"])
    if match is None:
        return []
    return re.findall(r"'([a-z_]+)'", match.group(1))


def emitted_subtypes(pack: str) -> set[str]:
    out = set()
    for entry in templates(pack).values():
        for node in entry["admission_policy"]["output_signature"]["nodes"]:
            out.add(node["subtype"])
    return out


# ---------------------------------------------------------------------------
# A local HTTP server for the sources that fetch live
# ---------------------------------------------------------------------------


class FixtureServer:
    """Serve registered responses on 127.0.0.1 so a source run by okg in a
    subprocess fetches fixtures instead of the network.

    `add(path, body)` answers GET <path> (any query) with `body`;
    `add(path, body, query={"k": "v"})` answers only when the query carries
    those values, and is tried before the query-free route.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[str, dict[str, str], bytes, str]] = []
        self.requests: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                server.requests.append(self.path)
                parts = urlsplit(self.path)
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                for path, want, body, ctype in sorted(
                    server.routes, key=lambda r: -len(r[1])
                ):
                    if path == parts.path and all(query.get(k) == v for k, v in want.items()):
                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def add(self, path: str, body, *, query: dict | None = None, ctype: str = "") -> None:
        if not isinstance(body, (bytes, str)):
            body = json.dumps(body)
            ctype = ctype or "application/json"
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.routes.append((path, dict(query or {}), body, ctype or "text/plain"))

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


# ---------------------------------------------------------------------------
# Fixture inputs, one function per context pack
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _docsite(data: Path, server: FixtureServer) -> dict:
    _write_json(
        data / "docsite" / "records.json",
        [
            {
                "title": "Getting started",
                "url": "https://docs.example.org/start.html",
                "body": "Install the client, then run the setup script. " * 20,
                "site_name": "example-docs",
                "source_repo": "example/docs",
                "path": "start.md",
            }
        ],
    )
    return {}


def _indico(data: Path, server: FixtureServer) -> dict:
    event = (TEST_FIXTURES / "indico" / "event_1716863_contributions.json").read_bytes()
    server.add("/export/event/1716863.json", event, ctype="application/json")
    return {"base_url": server.url, "event_ids": '["1716863"]'}


def _twiki(data: Path, server: FixtureServer) -> dict:
    root = data / "twiki-snapshot"
    (root / "Sub").mkdir(parents=True)
    (root / "TopicOne.txt").write_text(
        '%META:TOPICINFO{author="jdoe" date="1700000000" version="12"}%\n'
        '%META:TOPICPARENT{name="TopicTwo"}%\n'
        "---++ Topic One\nSee [[TopicTwo]] for the transfer procedure.\n",
        encoding="utf-8",
    )
    (root / "TopicTwo.txt").write_text(
        "---+ Topic Two\nBody two with [[Sub.SubTopicPage]].\n", encoding="utf-8"
    )
    (root / "Sub" / "SubTopicPage.txt").write_text(
        "---+ Sub Topic\nGrandchild body.\n", encoding="utf-8"
    )
    return {"eos_root": str(root)}


def _cds(data: Path, server: FixtureServer) -> dict:
    page1 = (TEST_FIXTURES / "cds_page1.xml").read_bytes()
    page2 = (TEST_FIXTURES / "cds_page2.xml").read_bytes()
    server.add("/oai2d", page1, ctype="text/xml")
    server.add("/oai2d", page2, query={"resumptionToken": "TOKEN-PAGE-2"}, ctype="text/xml")
    return {"base_url": server.url + "/oai2d", "sets": '["cerncds:cms-pas"]'}


def _jira(data: Path, server: FixtureServer) -> dict:
    records = [
        {
            "key": "DEMO-1",
            "summary": "Transfer failures at the test site",
            "description": "Stuck transfers since the upgrade; see DEMO-2.",
            "project": "DEMO",
            "status": "Open",
            "priority": "Critical",
            "issue_type": "Bug",
            "assignee": "Ada Lovelace",
            "reporter": "Grace Hopper",
            "created": "2026-01-05T10:00:00.000+0000",
            "updated": "2026-02-01T09:30:00.000+0000",
        },
        {"key": "DEMO-2", "summary": "Upgrade the storage servers", "project": "DEMO"},
    ]
    _write_json(data / "jira" / "records.json", records)
    _write_json(data / "jira" / "meta.json", {"record_count": len(records)})
    return {}


def _git_code(data: Path, server: FixtureServer) -> dict:
    upstream = data.parent / "upstream" / "demo"
    if not (upstream / ".git").is_dir():
        (upstream / "pkg").mkdir(parents=True)
        (upstream / "pkg" / "transfer.py").write_text(
            '"""Transfer helpers."""\n\n\n'
            "def retry(job, attempts=3):\n"
            '    """Retry a job a few times."""\n'
            "    for _ in range(attempts):\n"
            "        if submit(job):\n"
            "            return True\n"
            "    return False\n\n\n"
            "def submit(job):\n"
            "    return bool(job)\n",
            encoding="utf-8",
        )
        (upstream / "README.md").write_text("# demo\n\nA test repository.\n", encoding="utf-8")
        git(["init", "-q", "-b", "main"], upstream)
        git(["add", "-A"], upstream)
        git(["commit", "-q", "-m", "first commit"], upstream)
        git(["tag", "v0.1.0"], upstream)
    return {"repo_name": "demo", "repo_url": str(upstream)}


FIXTURES: dict[str, Callable[[Path, FixtureServer], dict]] = {
    "docsite": _docsite,
    "indico": _indico,
    "twiki": _twiki,
    "cds": _cds,
    "jira": _jira,
    "git-code": _git_code,
    # Both read git-code's checkout; its fixture (run for the closure) is all
    # they need.
    "code-text": lambda data, server: {},
    "code-symbols": lambda data, server: {},
}


# ---------------------------------------------------------------------------
# Running okg
# ---------------------------------------------------------------------------


def okg_bin() -> str:
    import sys

    return str(Path(sys.executable).with_name("okg"))


def run_okg(args: list[str], *, cwd: Path, env: dict, timeout: int = 900):
    return subprocess.run(
        [okg_bin(), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout
    )


def git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=pack-test", "-c", "user.email=pack-test@example.org", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=False)
