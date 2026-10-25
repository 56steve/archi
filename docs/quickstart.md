# Quickstart

From an empty machine to a chat that answers from your team's knowledge
graph, with the `cern-team` bundle.

**You need:** `docker` or `podman` (with `compose`), Python 3.12+, and `git`.
No account, key or root access is needed.

```bash
python3.12 -m venv archi-venv && source archi-venv/bin/activate
pip install "okg @ https://github.com/mitdbg/akmon/releases/download/v0.2.0/okg-0.2.0-py3-none-any.whl" \
  "archi-kb @ git+https://github.com/archi-physics/archi@archi_v3#subdirectory=python"

archi init myteam --without docsite,indico --with cmssw-releases
                           # ./myteam: git repo, secrets, a published graph
cd myteam
archi up                   # graph and chat databases, MCP server, Open WebUI
archi doctor               # every check, with a fix line for each failure
```

archi-kb is not on PyPI yet, so it comes from git. Do not run
`pip install archi`: that name on PyPI is an unrelated package.

`archi up` prints the chat address, the admin account, and the MCP endpoint.
The passwords and the MCP bearer token are in `myteam/.env`.

## What each command does

**`archi init <name> [--bundle B] [--with P] [--answer ID=VALUE]`** creates
`./<name>/` as a git repository and commits a baseline. It renders the
bundle into `profiles/`, generates every secret into `.env` (mode 0600,
git-ignored), and writes `archi.yaml` and `compose.yaml`. Then it starts the
graph database and runs `okg install`, commits the deployment, publishes
with `okg provision`, and sets okg's role passwords. Every path it passes to
okg is absolute. The data root, where connector caches and repository
checkouts go, is `~/.local/share/archi/<name>` (`--data-root` to change it)
and must lie outside the instance. The default bundle is `cern-team`. Its default
packs, docsite and indico, need a records cache and an Indico event or
category; until a default that needs neither ships, select what you can feed,
for example the public CMSSW release catalog:
`archi init <name> --without docsite,indico --with cmssw-releases`.

**`archi up`** starts one compose project, `archi-<name>`, on one network:

| Service | What | Published on |
|---|---|---|
| `graph-db` | AKMON Postgres, with okg's preload libraries | 127.0.0.1, for okg on the host |
| `chat-db` | Open WebUI's own Postgres | not published |
| `mcp` | `okg mcp-serve`, read-only, bearer token | 127.0.0.1 |
| `webui` | Open WebUI v0.11.4 | 127.0.0.1 |

It then signs in to the site as its admin (creating the account on first
start), points it at a local ollama if one answers on port 11434
(`--ollama-url` for another), and runs `okg chat sync`, which registers the
graph tools and the preset and proves both. In the chat, pick the preset
named after the site: the graph tools are bound to it, not to the raw model.

**`archi doctor [--json]`** runs `okg deployment lint`, `okg doctor`, and
one check for each quickstart trap archi absorbs, and prints a fix line for
each failure. `--json` prints one object for agents. Exit code 0 means no
check failed.

**`archi down`** stops the project and keeps every volume. `archi down
--purge --yes` also deletes the graph, chat and webui volumes; that is the
reset (okg keeps its ownership claim in the database, so deleting the
folder alone is not one).

## Adding sources

`archi add <pack> --answer ID=VALUE` adds a pack to a running instance and
publishes again. See [packs and bundles](packs-and-bundles.md).

## A local model

The chat needs a model server. With ollama on the same machine, `archi up`
finds it. Two things to know:

- Set the model at init: `archi init myteam --answer chat_model=<a model
  ollama list shows>`. The bundle default is `llama3.1:8b`.
- ollama's default context window (4096 tokens) is smaller than the preset's
  system prompt plus the graph tools' descriptions, so ollama cuts the prompt
  and the model never sees its tools. Start ollama with
  `OLLAMA_CONTEXT_LENGTH=16384` or more, or set `num_ctx` in the chat's
  controls.

Any MCP client can use the graph without a model in the chat: point it at the
MCP endpoint `archi up` printed, with `OKG_CHAT_MCP_TOKEN` from `.env` as
the bearer token.

## A remote host

Every port is bound to 127.0.0.1. On a remote machine, `archi up` prints the
`ssh -L` command that tunnels the chat to your own browser.

## What changed from the long quickstart

The [long cern-team quickstart](cern-team-quickstart.md) is now reference
material. Each of its traps is listed in the
[footgun ledger](golden-path-footguns.md), as absorbed by archi or filed
with AKMON.
