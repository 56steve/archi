"""`archi connect open-webui|claude-code|codex|openclaw|mcp` (PACT r3-mcp-clients req.connect).

    archi connect claude-code [--instance DIR] [--print | --write [PATH]] [--show-token] [--token-ref]

Writes the config another agent needs to use the instance's MCP endpoint
(`http://127.0.0.1:<ports.mcp>/mcp`, streamable HTTP, bearer token
`OKG_CHAT_MCP_TOKEN` from the instance `.env`) and every external server the
instance's `mcp_servers:` block names (archi.yaml).

Each client's real format (checked 2026-10; see docs/connecting-clients.md):

    claude-code  .mcp.json            {"mcpServers": {name: {"type": "http", "url", "headers"}}}
                                      (what `claude mcp add --transport http --header` writes)
    codex        ~/.codex/config.toml [mcp_servers.<name>] url / bearer_token_env_var / env_http_headers.
                                      Codex speaks streamable HTTP itself: no stdio bridge.
    openclaw     ~/.openclaw/openclaw.json   mcp.servers.<name> {url, transport: "streamable-http", headers}
                                      from OpenClaw's configuration docs; not run against a live OpenClaw
    mcp          generic {"mcpServers": ...} JSON for any client that reads that shape
    open-webui   nothing to write: `archi up` registers the server (okg chat sync); prints what it did

The token is never printed unless `--show-token`. Printed configs carry
`${OKG_CHAT_MCP_TOKEN}` (every client above expands it) and a written file
carries the token (mode 0600) unless `--token-ref`. A client config that
already exists is never replaced: archi's entries are merged into it, and
running the command again changes nothing.

External servers' secrets are environment-variable names in archi.yaml; they are
written as references (`${NAME}`, or Codex's `env_http_headers`), never as values.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from archi.stack.instance import Instance, InstanceError, McpServer, read_env

CLIENTS = ("open-webui", "claude-code", "codex", "openclaw", "mcp")
TOKEN_ENV = "OKG_CHAT_MCP_TOKEN"
DEFAULT_PATHS = {
    "claude-code": "./.mcp.json",
    "codex": "~/.codex/config.toml",
    "openclaw": "~/.openclaw/openclaw.json",
    "mcp": "./archi-mcp.json",
}
OPENCLAW_NOTE = (
    "OpenClaw's format is taken from its documentation (mcp.servers, transport "
    "\"streamable-http\"); archi has not run it against a live OpenClaw. Check it with "
    "`openclaw mcp show`."
)


class ConnectError(Exception):
    """A message for the operator, printed as `error: ...`."""


@dataclass
class Entry:
    """One MCP server as a client config holds it. `headers` values are literal or `${VAR}` text."""

    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    #: set for the instance server: the header value is `Bearer <token env var>`
    token_env: Optional[str] = None
    #: external servers: {header: env var name} (the variable holds the whole header value)
    env_headers: dict[str, str] = field(default_factory=dict)


def server_name(instance: Instance) -> str:
    return f"archi-{instance.name}"


def mcp_url(instance: Instance) -> str:
    return f"http://127.0.0.1:{instance.ports.mcp}/mcp"


def entries(instance: Instance, token: str, *, literal: bool) -> list[Entry]:
    """The instance server first, then the external ones. `literal` puts the token in."""
    bearer = f"Bearer {token}" if literal else f"Bearer ${{{TOKEN_ENV}}}"
    out = [Entry(server_name(instance), mcp_url(instance), {"Authorization": bearer}, token_env=TOKEN_ENV)]
    for ext in instance.mcp_servers:
        out.append(Entry(
            ext.name, ext.url, {h: f"${{{var}}}" for h, var in ext.headers.items()},
            env_headers=dict(ext.headers),
        ))
    return out


# --- renderers ------------------------------------------------------------------------------


def json_servers(entry_list: list[Entry], *, openclaw: bool) -> dict[str, dict[str, Any]]:
    servers: dict[str, dict[str, Any]] = {}
    for e in entry_list:
        if openclaw:
            body: dict[str, Any] = {"url": e.url, "transport": "streamable-http"}
        else:
            body = {"type": "http", "url": e.url}
        if e.headers:
            body["headers"] = dict(e.headers)
        servers[e.name] = body
    return servers


def json_document(client: str, entry_list: list[Entry]) -> dict[str, Any]:
    if client == "openclaw":
        return {"mcp": {"servers": json_servers(entry_list, openclaw=True)}}
    return {"mcpServers": json_servers(entry_list, openclaw=False)}


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def toml_block(entry: Entry) -> str:
    """One `[mcp_servers.<name>]` table. Codex reads secrets from the environment by name."""
    lines = [f"[mcp_servers.{entry.name}]", f"url = {_q(entry.url)}"]
    if entry.token_env:
        lines.append(f"bearer_token_env_var = {_q(entry.token_env)}")
    if entry.env_headers:
        pairs = ", ".join(f"{_q(h)} = {_q(v)}" for h, v in entry.env_headers.items())
        lines.append(f"env_http_headers = {{ {pairs} }}")
    return "\n".join(lines) + "\n"


def toml_document(entry_list: list[Entry]) -> str:
    return "\n".join(toml_block(e) for e in entry_list)


def claude_add_commands(entry_list: list[Entry]) -> list[str]:
    cmds = []
    for e in entry_list:
        parts = ["claude mcp add --transport http", e.name, e.url]
        parts += [f'--header "{h}: {v}"' for h, v in e.headers.items()]
        cmds.append(" ".join(parts))
    return cmds


# --- merge ----------------------------------------------------------------------------------


def merge_json(existing: Optional[str], client: str, entry_list: list[Entry], source: str) -> tuple[str, str]:
    """Merge archi's servers into a JSON client config. Returns (text, "added"|"updated"|"unchanged")."""
    wanted = json_servers(entry_list, openclaw=client == "openclaw")
    if existing is None or not existing.strip():
        doc: Any = {}
    else:
        try:
            doc = json.loads(existing)
        except json.JSONDecodeError as exc:
            raise ConnectError(
                f"{source} is not strict JSON ({exc}); archi will not rewrite it. Merge this by hand "
                f"(`archi connect {client} --print`)"
                + (f". {OPENCLAW_NOTE}" if client == "openclaw" else "")
            ) from exc
        if not isinstance(doc, dict):
            raise ConnectError(f"{source}: top level is not an object")
    if client == "openclaw":
        mcp = doc.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            raise ConnectError(f"{source}: `mcp` is not an object")
        servers = mcp.setdefault("servers", {})
    else:
        servers = doc.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ConnectError(f"{source}: the MCP servers entry is not an object")
    before = {name: servers.get(name) for name in wanted}
    existed = any(v is not None for v in before.values())
    servers.update(wanted)
    status = "unchanged" if before == wanted else ("updated" if existed else "added")
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n", status


_HEADER = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _is_archi_table(header: str, names: set[str]) -> bool:
    parts = [p.strip().strip('"') for p in header.split(".")]
    return len(parts) >= 2 and parts[0] == "mcp_servers" and parts[1] in names


def merge_toml(existing: Optional[str], entry_list: list[Entry], source: str) -> tuple[str, str]:
    """Replace archi's `[mcp_servers.<name>]` tables in a Codex config; keep every other line."""
    names = {e.name for e in entry_list}
    block = toml_document(entry_list)
    text = existing or ""
    if text.strip():
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConnectError(f"{source} is not valid TOML ({exc}); fix it first, archi will not rewrite it") from exc
    kept: list[str] = []
    dropping = False
    for line in text.splitlines(keepends=True):
        m = _HEADER.match(line)
        if m:
            dropping = _is_archi_table(m.group(1), names)
        if not dropping:
            kept.append(line)
    body = "".join(kept).rstrip("\n")
    new = (body + "\n\n" if body else "") + block
    try:
        parsed = tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
        raise ConnectError(f"merging into {source} produced invalid TOML: {exc}") from exc
    if not names <= set(parsed.get("mcp_servers", {})):  # pragma: no cover - defensive
        raise ConnectError(f"merging into {source} lost an entry")
    if text == new:
        status = "unchanged"
    else:
        status = "updated" if any(f"mcp_servers.{n}]" in text for n in names) else "added"
    return new, status


# --- files ----------------------------------------------------------------------------------


def write_file(path: Path, text: str, *, secret: bool) -> list[str]:
    """Write atomically. A new file is 0600; an existing one keeps its mode unless it now holds a
    secret and is readable by others, which is tightened to 0600. Returns notes for the operator."""
    notes: list[str] = []
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
        if secret and mode & 0o077:
            notes.append(f"{path} was mode {mode:o}; set to 600 because it now holds the token")
            mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return notes


# --- commands -------------------------------------------------------------------------------


def instance_token(instance: Instance) -> str:
    secrets = read_env(instance.env_path)
    if not secrets.get(TOKEN_ENV):
        raise ConnectError(f"{instance.env_path} has no {TOKEN_ENV}; `archi init` did not finish")
    return secrets[TOKEN_ENV]


def print_config(client: str, entry_list: list[Entry], token: str, show_token: bool) -> str:
    if client == "codex":
        out = toml_document(entry_list)
        hint = f"# Codex reads the token from the environment: export {TOKEN_ENV}=" + (
            token if show_token else "<token from the instance .env>")
        for e in entry_list[1:]:
            for var in e.env_headers.values():
                hint += f"\n# and {var} for {e.name}"
        return out + "\n" + hint + "\n"
    doc = json.dumps(json_document(client, entry_list), indent=2, ensure_ascii=False) + "\n"
    if client == "claude-code":
        doc += "\n# or, with the claude CLI:\n" + "\n".join(f"# {c}" for c in claude_add_commands(entry_list)) + "\n"
    if client == "openclaw":
        doc += f"\n# {OPENCLAW_NOTE}\n"
    return doc


def open_webui_report(instance: Instance) -> str:
    from archi.stack import compose as stack_compose

    if not (instance.deployment_dir / "deployment.yaml").is_file():
        raise ConnectError(f"{instance.deployment_dir}/deployment.yaml does not exist; run `archi init` first")
    port = stack_compose.mcp_port(instance.deployment_dir)
    lines = [
        "Open WebUI needs no config file: `archi up` registers the MCP server through `okg chat sync`.",
        f"  site            http://127.0.0.1:{instance.ports.webui}",
        f"  registered URL  http://mcp:{port}/mcp   (the site reaches the server by its service name)",
        f"  from the host   {mcp_url(instance)}",
        f"  auth            bearer; the token is {TOKEN_ENV} in {instance.env_path}",
        "  where           the preset named after the site in the model picker has the graph tools bound",
        "If `archi up` has not run (or ran with --no-sync), run it; it syncs again each time.",
    ]
    if instance.mcp_servers:
        lines.append("External servers in archi.yaml are not synced to Open WebUI. Add them by hand under "
                     "Settings > Admin > External Tools:")
        for ext in instance.mcp_servers:
            names = ", ".join(f"{h} <- ${v}" for h, v in ext.headers.items()) or "no headers"
            lines.append(f"  {ext.name}  {ext.url}  ({names})")
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace, out=None) -> int:
    out = out or sys.stdout
    instance = Instance.load(args.instance)
    client = args.client
    if client == "open-webui":
        if args.write is not None:
            raise ConnectError("open-webui has no config file to write; `archi up` registers the server")
        out.write(open_webui_report(instance))
        return 0
    token = instance_token(instance)
    writing = args.write is not None
    # A printed config carries a reference unless asked; a written file carries the token unless
    # --token-ref. Codex never holds the token: it names the environment variable.
    literal = args.show_token if not writing else not args.token_ref
    entry_list = entries(instance, token, literal=literal or False)
    if not writing:
        out.write(print_config(client, entry_list, token, args.show_token))
        return 0
    path = Path(args.write if args.write != "" else DEFAULT_PATHS[client]).expanduser()
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    if client == "codex":
        text, status = merge_toml(existing, entry_list, str(path))
    else:
        text, status = merge_json(existing, client, entry_list, str(path))
    holds_token = literal and client != "codex"
    if status != "unchanged" or (holds_token and path.is_file()):
        notes = write_file(path, text, secret=holds_token)
    else:
        notes = []
    names = ", ".join(e.name for e in entry_list)
    out.write(f"{status}: {names} in {path}" + (" (mode 600)" if holds_token else "") + "\n")
    for note in notes:
        out.write(f"note: {note}\n")
    if client == "codex":
        out.write(f"note: Codex reads the token from the environment: export {TOKEN_ENV}=<token from "
                  f"{instance.env_path}>\n")
    if client == "openclaw":
        out.write(f"note: {OPENCLAW_NOTE}\n")
    if holds_token:
        out.write("note: this file holds the token; do not commit it\n")
    if args.show_token:
        out.write(f"token: {token}\n")
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    try:
        return run(args)
    except (ConnectError, InstanceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def add_parser(sub) -> None:
    p = sub.add_parser("connect", help="write another agent's MCP config for this instance")
    p.add_argument("client", choices=CLIENTS)
    p.add_argument("--instance", type=Path, default=Path("."), help="instance directory")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--print", dest="print_", action="store_true", help="print the config (the default)")
    mode.add_argument("--write", nargs="?", const="", default=None, metavar="PATH",
                      help="merge into this client config (default per client: "
                           + ", ".join(f"{k} {v}" for k, v in DEFAULT_PATHS.items()) + ")")
    p.add_argument("--show-token", action="store_true", help="include the bearer token in what is printed")
    p.add_argument("--token-ref", action="store_true",
                   help="with --write: write ${%s} instead of the token" % TOKEN_ENV)
    p.set_defaults(func=cmd_connect)
