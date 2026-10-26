"""A small tool-calling agent over an instance's MCP endpoint.

The loop is the whole agent: send the question and the MCP tools to the model,
run the tool calls it asks for, feed the results back, stop when it answers.
The model comes from an OpenAI-compatible chat endpoint (Ollama's `/v1`); the
tools come from the instance's streamable-HTTP MCP server with its bearer
token, the supported surface (ADR 0002 D5). Both sit behind two small
interfaces so the tests run with a fake model and a fake MCP and no network.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol
from urllib.parse import urlparse

SYSTEM_PROMPT = """\
You answer questions about a knowledge graph by calling its tools. Search the \
graph, read what you need, and answer only from what the tools return. If the \
tools return nothing relevant, say so.

End your answer with a line "Sources:" listing the node ids you used. Copy each \
id in full, with the part before the colon, exactly as the tool results show it \
(for example documentation_page:22 or meeting_minutes:1716863). Do not invent ids."""

DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_TOOL_CHARS = 6000
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class ModelError(RuntimeError):
    """The chat endpoint could not answer."""


class ToolError(RuntimeError):
    """The MCP server could not be reached or listed its tools."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ChatReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ChatModel(Protocol):
    name: str

    def complete(self, messages: list[dict], tools: list[dict]) -> ChatReply: ...


class ToolBox(Protocol):
    async def list_tools(self) -> list[ToolSpec]: ...

    async def call(self, name: str, arguments: Mapping[str, Any]) -> tuple[str, bool]:
        """The tool's text and whether it reported an error."""
        ...


def prompt_hash(tools: list[ToolSpec]) -> str:
    """One digest over the system prompt and the tool schemas the model saw."""
    body = json.dumps(
        {"system": SYSTEM_PROMPT, "tools": [[t.name, t.description, t.input_schema] for t in tools]},
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def openai_tools(tools: list[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": dict(t.input_schema) or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def require_local(url: str) -> None:
    """A smoke run costs nothing: refuse a model endpoint off this host or private network."""
    host = (urlparse(url).hostname or "").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A bare name ("ollama", a compose service) or a .local / .localhost name.
        if host in LOCAL_HOSTS or "." not in host or host.endswith((".local", ".localhost")):
            return
    else:
        if address.is_loopback or address.is_private:
            return
    raise ModelError(
        f"model endpoint {url} is not on this host or a private network; the smoke harness "
        "uses local models only (no paid calls). Pass --allow-remote-model to override."
    )


class OpenAIChat:
    """`POST {base_url}/chat/completions`, as Ollama and vLLM serve it."""

    def __init__(self, base_url: str, model: str, *, api_key: str = "ollama", temperature: float = 0.0,
                 timeout: float = 600.0, no_think: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.name = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.no_think = no_think

    def complete(self, messages: list[dict], tools: list[dict]) -> ChatReply:
        import requests

        body: dict[str, Any] = {
            "model": self.name,
            "messages": messages,
            "temperature": self.temperature,
            "seed": 0,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
        if self.no_think:
            body["reasoning_effort"] = "none"
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ModelError(f"{self.base_url}: {exc}") from exc
        if response.status_code != 200:
            raise ModelError(f"{self.base_url}: HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
            message = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise ModelError(f"{self.base_url}: unreadable reply: {response.text[:300]}") from exc
        calls = []
        for i, raw in enumerate(message.get("tool_calls") or []):
            fn = raw.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    args = {"_unparsed": args}
            calls.append(ToolCall(str(raw.get("id") or f"call_{i}"), str(fn.get("name") or ""), args))
        usage = payload.get("usage") or {}
        return ChatReply(
            content=message.get("content") or "",
            tool_calls=tuple(calls),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )


class McpToolBox:
    """The instance's MCP endpoint (streamable HTTP, bearer token), held open for a run."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self._stack = None
        self._session = None

    async def __aenter__(self) -> "McpToolBox":
        from contextlib import AsyncExitStack

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        stack = AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(self.url, headers={"Authorization": f"Bearer {self.token}"})
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException as exc:
            await stack.aclose()
            raise ToolError(f"{self.url}: {_describe(exc)}") from exc
        self._stack, self._session = stack, session
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    async def list_tools(self) -> list[ToolSpec]:
        result = await self._session.list_tools()
        return [ToolSpec(t.name, t.description or "", t.inputSchema or {}) for t in result.tools]

    async def call(self, name: str, arguments: Mapping[str, Any]) -> tuple[str, bool]:
        result = await self._session.call_tool(name, dict(arguments))
        text = "".join(getattr(c, "text", "") for c in result.content)
        return text, bool(result.isError)


def _describe(exc: BaseException) -> str:
    inner = getattr(exc, "exceptions", None)
    if inner:
        return "; ".join(_describe(e) for e in inner)
    return f"{type(exc).__name__}: {exc}"


@dataclass
class ToolRecord:
    name: str
    arguments: Mapping[str, Any]
    chars: int
    is_error: bool


@dataclass
class Trace:
    """What one question's run did."""

    answer: str = ""
    tool_calls: list[ToolRecord] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    steps: int = 0
    seconds: float = 0.0
    #: the text of every tool result as the model saw it, for reading node ids out of it
    results: list[str] = field(default_factory=list)
    error: Optional[str] = None
    finished: bool = False


async def ask(model: ChatModel, tools: ToolBox, specs: list[ToolSpec], question: str, *,
              max_steps: int = DEFAULT_MAX_STEPS, max_tool_chars: int = DEFAULT_MAX_TOOL_CHARS) -> Trace:
    """Run the loop for one question. A model failure ends it with `error` set."""
    trace = Trace()
    started = time.monotonic()
    schema = openai_tools(specs)
    known = {s.name for s in specs}
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    try:
        for _ in range(max_steps):
            trace.steps += 1
            reply = model.complete(messages, schema)
            trace.prompt_tokens += reply.prompt_tokens
            trace.completion_tokens += reply.completion_tokens
            if not reply.tool_calls:
                trace.answer = reply.content
                trace.finished = True
                break
            messages.append({
                "role": "assistant",
                "content": reply.content,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))}}
                    for c in reply.tool_calls
                ],
            })
            for call in reply.tool_calls:
                if call.name not in known:
                    text, is_error = f"error: no tool named {call.name!r}; tools are {sorted(known)}", True
                else:
                    try:
                        text, is_error = await tools.call(call.name, call.arguments)
                    except Exception as exc:  # noqa: BLE001 - a failing tool is data for the model
                        text, is_error = f"error: {_describe(exc)}", True
                trace.tool_calls.append(ToolRecord(call.name, dict(call.arguments), len(text), is_error))
                shown = text if len(text) <= max_tool_chars else text[:max_tool_chars] + "\n[cut]"
                trace.results.append(shown)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": shown})
    except ModelError as exc:
        trace.error = str(exc)
    trace.seconds = time.monotonic() - started
    return trace
