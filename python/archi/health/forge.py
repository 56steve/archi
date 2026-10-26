"""GitLab and GitHub issue trackers, over their REST APIs.

These reach an external system, so ADR 0002 section 7.2 allows them only inside a live tool
(`archi.tools.instance_health`), which the runtime runs at L2 after an approval. Importing this module
does nothing; building a tracker reads the token from the environment variable the instance config
names (`token_env`). The token is a header value only: it is never logged, put in an error message, a
URL or a returned result.

How a problem finds its issue. Every issue the automation makes carries the label `archi-health` and,
last in its body, a marker comment `<!-- archi-health key=<key> family=<family> -->`. A run lists the
issues with that label (all states, every page) once, and matches by the marker's key. An issue without
the marker is not ours and is ignored, whatever its label. A closed issue whose problem returns is
reopened, not duplicated.

The HTTP session is a seam: any object with `request(method, url, headers=, json=, params=, timeout=)`
returning a response with `status_code`, `json()` and `text` (a `requests.Session` does). The unit tests
pass a fake and never touch a network.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Mapping, Optional

from archi.health.problems import Problem
from archi.health.tracker import LABEL, MARKER, OPEN, RESOLVED, Issue, TrackerError, render_body

PAGE = 100
TIMEOUT = 30
MAX_PAGES = 50


def with_marker(problem: Problem) -> str:
    return f"{render_body(problem).rstrip()}\n\n<!-- archi-health key={problem.key} family={problem.family} -->\n"


def strip_marker(body: str) -> str:
    return MARKER.sub("", body or "").rstrip() + "\n"


class _Forge:
    """Shared by the two forges: the listing cache and the request helper."""

    kind = ""

    def __init__(self, base: str, token: str, session: Any = None, *, timeout: int = TIMEOUT):
        if not token:
            raise TrackerError(f"the {self.kind} token is empty")
        if session is None:
            import requests

            session = requests.Session()
        self.base = base.rstrip("/")
        self._token = token
        self.session = session
        self.timeout = timeout
        self._issues: Optional[dict[str, tuple[Issue, Any]]] = None  # key -> (issue, forge id)

    # subclasses
    def headers(self) -> dict[str, str]: ...
    def list_url(self) -> str: ...
    def parse(self, item: Mapping[str, Any]) -> Optional[tuple[Issue, Any]]: ...
    def create_request(self, problem: Problem) -> tuple[str, dict]: ...
    def update_request(self, ident: Any, problem: Problem, reopen: bool) -> tuple[str, str, dict]: ...
    def close(self, ident: Any, note: str) -> None: ...

    def request(self, method: str, url: str, *, json: Any = None, params: Any = None) -> Any:
        try:
            response = self.session.request(method, url, headers=self.headers(), json=json, params=params,
                                            timeout=self.timeout)
        except Exception as exc:  # a network error: say which call, never include the headers
            raise TrackerError(f"{self.kind} {method} {self._redact(url)} failed: {type(exc).__name__}") from exc
        status = response.status_code
        if not 200 <= status < 300:
            snippet = self._redact((getattr(response, "text", "") or "")[:200]).replace("\n", " ")
            raise TrackerError(f"{self.kind} {method} {self._redact(url)} returned {status}: {snippet}")
        try:
            return response.json()
        except ValueError as exc:
            raise TrackerError(f"{self.kind} {method} {self._redact(url)} returned {status} but not JSON") from exc

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    # --- the listing ---------------------------------------------------------

    def _load(self) -> dict[str, tuple[Issue, Any]]:
        if self._issues is not None:
            return self._issues
        found: dict[str, tuple[Issue, Any]] = {}
        for page in range(1, MAX_PAGES + 1):
            items = self.request("GET", self.list_url(),
                                 params={"labels": LABEL, "state": "all", "per_page": PAGE, "page": page})
            if not isinstance(items, list):
                raise TrackerError(f"{self.kind} issue list was not a list")
            for item in items:
                parsed = self.parse(item)
                if parsed is None:
                    continue
                issue, ident = parsed
                if issue.key in found:
                    raise TrackerError(f"{self.kind}: two issues carry the key {issue.key!r} "
                                       f"({found[issue.key][0].ref} and {issue.ref}); close one by hand")
                found[issue.key] = (issue, ident)
            if not items:  # a server may return fewer than asked without it being the last page
                break
        else:
            raise TrackerError(f"{self.kind}: more than {MAX_PAGES * PAGE} labelled issues; refusing to guess")
        self._issues = found
        return found

    def open_issues(self) -> list[Issue]:
        return [i for i, _ in self._load().values() if i.state == OPEN]

    def find(self, key: str) -> Optional[Issue]:
        entry = self._load().get(key)
        return entry[0] if entry else None

    def create(self, problem: Problem) -> Issue:
        self._load()
        url, body = self.create_request(problem)
        parsed = self.parse(self.request("POST", url, json=body))
        if parsed is None:
            raise TrackerError(f"{self.kind} did not return the issue it created for {problem.key}")
        self._issues[problem.key] = parsed
        return parsed[0]

    def update(self, issue: Issue, problem: Problem) -> Issue:
        _, ident = self._load()[issue.key]
        method, url, body = self.update_request(ident, problem, reopen=issue.state != OPEN)
        parsed = self.parse(self.request(method, url, json=body))
        if parsed is None:
            raise TrackerError(f"{self.kind} did not return the issue it updated for {problem.key}")
        self._issues[problem.key] = parsed
        return parsed[0]

    def resolve(self, issue: Issue, note: str) -> Issue:
        _, ident = self._load()[issue.key]
        self.close(ident, note)
        closed = Issue(issue.key, issue.family, issue.title, issue.body, RESOLVED, issue.ref)
        self._issues[issue.key] = (closed, ident)
        return closed

    def finish(self) -> None:
        return None


class GitLabTracker(_Forge):
    """GitLab: `<url>/api/v4/projects/<id or path>/issues`, token in the `PRIVATE-TOKEN` header."""

    kind = "gitlab"

    def __init__(self, url: str, project: str, token: str, session: Any = None, **kw: Any):
        super().__init__(f"{url.rstrip('/')}/api/v4", token, session, **kw)
        self.project = urllib.parse.quote(str(project), safe="")

    def headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token, "Content-Type": "application/json"}

    def list_url(self) -> str:
        return f"{self.base}/projects/{self.project}/issues"

    def parse(self, item: Mapping[str, Any]) -> Optional[tuple[Issue, Any]]:
        match = MARKER.search(item.get("description") or "")
        if match is None:
            return None
        state = OPEN if item.get("state") == "opened" else RESOLVED
        return (Issue(match.group(1), match.group(2), item.get("title", ""), strip_marker(item.get("description")),
                      state, ref=item.get("web_url") or str(item.get("iid"))), item["iid"])

    def create_request(self, problem: Problem) -> tuple[str, dict]:
        return self.list_url(), {"title": problem.title, "description": with_marker(problem),
                                 "labels": f"{LABEL},{LABEL}:{problem.family}"}

    def update_request(self, ident: Any, problem: Problem, reopen: bool) -> tuple[str, str, dict]:
        body: dict[str, Any] = {"title": problem.title, "description": with_marker(problem)}
        if reopen:
            body["state_event"] = "reopen"
        return "PUT", f"{self.list_url()}/{ident}", body

    def close(self, ident: Any, note: str) -> None:
        self.request("POST", f"{self.list_url()}/{ident}/notes", json={"body": note})
        self.request("PUT", f"{self.list_url()}/{ident}", json={"state_event": "close"})


class GitHubTracker(_Forge):
    """GitHub: `<api>/repos/<owner>/<name>/issues`, token as a Bearer header."""

    kind = "github"

    def __init__(self, repo: str, token: str, session: Any = None, *, api_url: str = "https://api.github.com", **kw: Any):
        if repo.count("/") != 1 or not all(repo.split("/")):
            raise TrackerError(f"github repo {repo!r} must be owner/name")
        super().__init__(api_url, token, session, **kw)
        self.repo = repo

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def list_url(self) -> str:
        return f"{self.base}/repos/{self.repo}/issues"

    def parse(self, item: Mapping[str, Any]) -> Optional[tuple[Issue, Any]]:
        if "pull_request" in item:  # the issues endpoint also lists pull requests
            return None
        match = MARKER.search(item.get("body") or "")
        if match is None:
            return None
        state = OPEN if item.get("state") == "open" else RESOLVED
        return (Issue(match.group(1), match.group(2), item.get("title", ""), strip_marker(item.get("body")), state,
                      ref=item.get("html_url") or str(item.get("number"))), item["number"])

    def create_request(self, problem: Problem) -> tuple[str, dict]:
        return self.list_url(), {"title": problem.title, "body": with_marker(problem),
                                 "labels": [LABEL, f"{LABEL}:{problem.family}"]}

    def update_request(self, ident: Any, problem: Problem, reopen: bool) -> tuple[str, str, dict]:
        body: dict[str, Any] = {"title": problem.title, "body": with_marker(problem)}
        if reopen:
            body["state"] = "open"
        return "PATCH", f"{self.list_url()}/{ident}", body

    def close(self, ident: Any, note: str) -> None:
        self.request("POST", f"{self.list_url()}/{ident}/comments", json={"body": note})
        self.request("PATCH", f"{self.list_url()}/{ident}", json={"state": "closed", "state_reason": "completed"})
