# Connecting other agents (`archi connect`)

Open WebUI is one client of an instance. Any agent that speaks MCP over
streamable HTTP can use the same graph tools. The endpoint is
`http://127.0.0.1:<ports.mcp>/mcp`; the bearer token is `OKG_CHAT_MCP_TOKEN` in
the instance `.env`.

```console
archi connect claude-code [--instance DIR] [--print | --write [PATH]] [--show-token] [--token-ref]
```

`--print` (the default) shows the config. `--write` merges it into the client's
config file (default path below). A config file that exists is never replaced:
archi adds or updates its own entries (`archi-<instance name>` and the external
servers) and keeps everything else. Running the same command again changes
nothing. A file that archi cannot parse is left alone and the command stops.

The token is not printed unless you pass `--show-token`. Printed configs carry
`${OKG_CHAT_MCP_TOKEN}`. A written file holds the token and is mode 0600 (an
existing file that is more open is tightened); pass `--token-ref` to write the
reference instead and export the variable in the shell that starts the client.

| Client | Default file | Format | Checked how |
|---|---|---|---|
| `claude-code` | `./.mcp.json` | `mcpServers.<name>` with `type: "http"`, `url`, `headers` | Matches what `claude mcp add --transport http NAME URL --header "H: V"` writes (run against the installed CLI). |
| `codex` | `~/.codex/config.toml` | `[mcp_servers.<name>]` with `url`, `bearer_token_env_var`, `env_http_headers` | Codex's MCP documentation. Codex speaks streamable HTTP itself; no stdio bridge such as `mcp-remote` is needed. Codex reads the token from the environment: `export OKG_CHAT_MCP_TOKEN=...`. Not run against a Codex binary. |
| `openclaw` | `~/.openclaw/openclaw.json` | `mcp.servers.<name>` with `url`, `transport: "streamable-http"`, `headers` | OpenClaw's configuration documentation only; **not run against a live OpenClaw**. The file is JSON5: archi merges only strict JSON and otherwise tells you to merge by hand. Check with `openclaw mcp show`. |
| `mcp` | `./archi-mcp.json` | `mcpServers.<name>` JSON | The shape most MCP clients read; for any client not listed. |
| `open-webui` | none | none | `archi up` already registered the server (`okg chat sync`); the command prints the URL the site uses (`http://mcp:<port>/mcp`), the site URL and where the token is. |

## External MCP servers

Declare them in the instance's `archi.yaml`:

```yaml
mcp_servers:
  - name: notes                       # lowercase letters, digits, - _
    url: https://notes.example.org/mcp
    headers:
      Authorization: NOTES_AUTH       # header name -> NAME of an environment variable
```

The variable holds the whole header value (for example `Bearer abc`). The file is
committed, so it never holds a secret; a value that is not a variable name, a URL
with credentials or a query string, an unknown key, a duplicate name or the
instance's own server name stops `archi connect` with the reason.

`archi connect` writes these entries next to the instance's own, as `${NOTES_AUTH}`
references (Codex: `env_http_headers`). Export the variable before starting the
client. Open WebUI is not synced from this block; the command lists them to add
under Settings > Admin > External Tools.

## Checking a recipe without a model

`python/tests/connect/recipe_check.py` reads a written config, connects exactly as
the client would (URL and headers as written; `${VAR}` and Codex's variables come
from the environment or `--env-file DIR/.env`; an unset variable is an error),
calls `search` on the instance server and `echo` on any external server that has
one, and prints a JSON result. Exit 0 means every call succeeded.

```console
archi connect claude-code --write /tmp/probe/.mcp.json
python python/tests/connect/recipe_check.py --client claude-code \
    --config /tmp/probe/.mcp.json --env-file DIR/.env --query "CMSSW"
```

`python/tests/connect/fixtures/echo_server.py` is a small external server (one
`echo` tool) for trying the `mcp_servers:` path.
