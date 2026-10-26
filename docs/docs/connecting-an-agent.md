# Connecting an agent

Open WebUI is one built-in client. It is not the product. The supported
surface is the instance's **MCP endpoint**. MCP (Model Context Protocol) is
the standard way for an agent to call tools. Anything that speaks MCP can
read the graph: Claude Code, codex, openclaw or your own harness.

## The endpoint

`archi up` prints the endpoint. It is on the local machine only:

```
http://127.0.0.1:<port>/mcp
```

The port is `ports.mcp` in the instance's `archi.yaml`. The client sends the
instance's token as a bearer token. The token is `OKG_CHAT_MCP_TOKEN` in the
instance's `.env` file. Treat it as a password. Never commit it.

The server is read-only. It offers the tools `search`, `expand`, `inspect`,
`filter`, `map` and `aggregate`, and the instance's live tools. Every answer
is pinned to one published generation and cites record ids.

## archi connect

`archi connect <client>` writes the client configuration for you, for
`open-webui`, `claude-code`, `codex`, `openclaw` and `mcp` (generic MCP over
HTTP). It is part of the 3.0 plan (PACT `r3-mcp-clients`) and is not in this
branch yet. Until it is, set the client up by hand with the endpoint and
token above.

## By hand

For Claude Code, from the instance directory:

```bash
set -a; . ./.env; set +a
claude mcp add --transport http archi "http://127.0.0.1:<port>/mcp" \
  --header "Authorization: Bearer $OKG_CHAT_MCP_TOKEN"
```

For any other client, give it the URL and an `Authorization: Bearer ...`
header. Keep the token in an environment variable where the client allows it.

Check the connection by asking for something only the graph knows, then
confirm the answer cites record ids. The fifth command of the
[quickstart](quickstart.md) does the same from a script.

## Other MCP servers

An instance can declare other MCP servers next to its own, so one client
sees all of them. Archi registers them and runs none of them. This is also
part of `archi connect` and is not in this branch yet.

## The language model

Archi never stores a model key. The agent is configured with its own model.
With a local ollama, set `OLLAMA_CONTEXT_LENGTH` to 16384 or more, because
the default window cuts the tool descriptions. See
[what each command does](operating/commands.md#a-local-model).
