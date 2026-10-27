"""The raw-document baseline of `archi eval --context`: two agent arms (PACT r3-context-quality).

Both arms put the same model, under the same caps and prompt, in front of a question;
they differ only in where the knowledge is:

    agent-bash    one tool, bash, run in a bubblewrap sandbox: no network, the raw corpus
                  (`archi.evaluation.raw_corpus`) mounted read-only at /corpus, a tmpfs
                  HOME, an empty environment, rg, jq and python3 on PATH when the host has
                  them. Each command may run 120 s and print 64 KiB.
    agent-graph   the instance's MCP tools.

The prompt is one text with one paragraph, the one saying where the knowledge lives, that
differs between the arms (`PROMPT`, `KNOWLEDGE`).

Scoring is the same for both (`score_evidence`):

- evidence recall per try: the share of the question's gold records whose corpus path,
  natural key or node id appears in a successful tool response. Tool inputs and the final
  answer never count. A gold record with no files in the corpus (ops-record, a live
  source) is unavailable: it leaves the denominator, in both arms, and is listed;
- MRR: 1 / the rank of the first gold record among the distinct records the agent read,
  in the order it read them (a record is read when its path, its node id or a gold
  record's natural key appears in a successful response);
- the answer score: a judge model compares the answer with the question's answer key. It
  runs only with `--judge` and only for questions that have a key; with no keys at all the
  report says "no answer keys", never 0.

A gold id is resolved to its record through the MCP `inspect` tool (its source and node
id, matched against the corpus's keys.json), or through the question's `gold_sources`. A
gold id that does not resolve refuses the run before any model call, with the id and the
reason: it is never scored as a miss.

Every model call (agent and judge) is written to a JSONL spend ledger with its tokens and
cost. The run refuses to start without a spend cap, and stops before a call whose cost
bound (input estimated from the request's size, output at most `max_output_tokens`) would
cross it; the report then says what completed.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from archi.evaluation.agent import ChatModel, ChatReply, ModelError, ToolSpec, _describe, openai_tools

AGENT_BASH, AGENT_GRAPH = "agent-bash", "agent-graph"
AGENT_ARMS = (AGENT_BASH, AGENT_GRAPH)
#: The ledger file name in a run's directory.
LEDGER = "ledger.jsonl"
DEFAULT_CAP_USD = 10.0
NO_ANSWER_KEYS = "no answer keys"
JUDGE_OFF = "judge off"
INSPECT = "inspect"
#: okg's JSON result format, as the no-model arms ask for it.
JSON_META = {"okg/result_format": "json"}


@dataclass(frozen=True)
class Caps:
    max_turns: int = 30
    max_tool_calls: int = 200
    try_seconds: float = 900.0
    command_seconds: float = 120.0
    #: the most of one tool response the model sees (and the scorer reads), both arms
    output_bytes: int = 64 * 1024


# --- the prompt ------------------------------------------------------------------------------

PROMPT = """\
You answer questions from the operators of a scientific computing facility.

{knowledge}

Find the records that answer the question, read them, and answer only from what you read. \
If what you find does not answer the question, say so rather than guess. You have at most \
{turns} turns and {calls} tool calls for this question.

End your answer with a line "Sources:" followed by the records you used."""

KNOWLEDGE = {
    AGENT_BASH: (
        "Where the knowledge is: the directory /corpus, read-only, holds the raw records this "
        "facility's knowledge graph was built from, one file per record, one folder per source "
        "({folders}). Your one tool, bash, runs a command in a sandbox with no network; {tools} "
        "A command may run for {seconds} seconds and print {kib} KiB; one that goes over either "
        "is stopped. Name each record you used by its file path."
    ),
    AGENT_GRAPH: (
        "Where the knowledge is: this facility's knowledge graph, which you reach through the "
        "tools you are given. Name each record you used by its id, as the tools show it."
    ),
}


def build_prompt(arm: str, caps: Caps, *, folders: Sequence[str] = (), tools: Sequence[str] = ()) -> str:
    if arm == AGENT_BASH:
        named = ", ".join(tools)
        tool_text = (f"{named} {'is' if len(tools) == 1 else 'are'} installed beside the usual shell commands."
                     if tools else "only the usual shell commands are installed.")
        knowledge = KNOWLEDGE[arm].format(folders=", ".join(folders) or "none", tools=tool_text,
                                          seconds=int(caps.command_seconds), kib=caps.output_bytes // 1024)
    else:
        knowledge = KNOWLEDGE[arm]
    return PROMPT.format(knowledge=knowledge, turns=caps.max_turns, calls=caps.max_tool_calls)


# --- money -----------------------------------------------------------------------------------


class SpendError(RuntimeError):
    """The run may not start: no usable spend cap or price."""


class CapReached(Exception):
    """The next model call could cross the spend cap."""


@dataclass(frozen=True)
class Price:
    """US dollars per million tokens."""

    input_per_m: float
    output_per_m: float
    cached_input_per_m: Optional[float] = None

    @classmethod
    def parse(cls, text: str) -> "Price":
        """`IN/OUT` or `IN/OUT/CACHED`, dollars per million tokens."""
        parts = str(text).split("/")
        try:
            values = [float(p) for p in parts]
        except ValueError:
            values = []
        if len(values) not in (2, 3) or not all(math.isfinite(v) and v >= 0 for v in values) or not any(values[:2]):
            raise SpendError(f"price {text!r} is not IN/OUT or IN/OUT/CACHED in dollars per million tokens")
        return cls(*values)

    def cost(self, prompt: int, completion: int, cached: int = 0) -> float:
        cached = max(0, min(cached, prompt))
        cached_rate = self.input_per_m if self.cached_input_per_m is None else self.cached_input_per_m
        return ((prompt - cached) * self.input_per_m + cached * cached_rate + completion * self.output_per_m) / 1e6

    def bound(self, prompt: int, completion: int) -> float:
        return (prompt * self.input_per_m + completion * self.output_per_m) / 1e6


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    """One JSONL row per model call. Refuses to exist without a cap."""

    def __init__(self, path: Path, cap_usd: Optional[float]) -> None:
        if cap_usd is None or isinstance(cap_usd, bool) or not isinstance(cap_usd, (int, float)) \
                or not math.isfinite(cap_usd) or cap_usd <= 0:
            raise SpendError(f"the agent arms refuse to start without a spend cap (got {cap_usd!r}); "
                             "pass --cap DOLLARS")
        self.path = Path(path)
        self.cap = float(cap_usd)
        self.spent = 0.0
        self.calls = 0
        self.stopped: Optional[str] = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def check(self, bound: float) -> None:
        if self.stopped:
            raise CapReached(self.stopped)
        if self.spent + bound > self.cap:
            self.stopped = (f"the next call could cost up to ${bound:.4f}, and ${self.spent:.4f} of the "
                            f"${self.cap:.2f} cap is spent")
            raise CapReached(self.stopped)

    def record(self, *, arm: str, role: str, question: str, attempt: int, model: str, reply: ChatReply,
               price: Price, bound: float) -> float:
        cost = price.cost(reply.prompt_tokens, reply.completion_tokens, reply.cached_tokens)
        self.spent += cost
        self.calls += 1
        row = {"at": _now(), "arm": arm, "role": role, "question": question, "try": attempt, "model": model,
               "prompt_tokens": reply.prompt_tokens, "cached_tokens": reply.cached_tokens,
               "completion_tokens": reply.completion_tokens, "reasoning_tokens": reply.reasoning_tokens,
               "cost_usd": round(cost, 6), "bound_usd": round(bound, 6), "spent_usd": round(self.spent, 6)}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self.spent > self.cap and not self.stopped:
            self.stopped = f"the spend reached ${self.spent:.4f}, over the ${self.cap:.2f} cap"
        return cost


def estimate_tokens(messages: list, tools: list) -> int:
    """A bound on the request's input tokens: one per three bytes of its JSON (BPE tokens
    average about four)."""
    return math.ceil(len(json.dumps([messages, tools]).encode("utf-8")) / 3)


@dataclass
class Metered:
    """A model behind the ledger: each call is checked against the cap first and recorded after."""

    model: ChatModel
    price: Price
    ledger: Ledger
    max_output_tokens: int = 16000

    def complete(self, messages: list, tools: list, *, arm: str, role: str, question: str, attempt: int,
                 seconds: Optional[float] = None) -> ChatReply:
        """`seconds` bounds the call's HTTP timeout (a try's time left), on a model that has one."""
        bound = self.price.bound(estimate_tokens(messages, tools), self.max_output_tokens)
        self.ledger.check(bound)
        saved = getattr(self.model, "timeout", None)
        if seconds is not None and isinstance(saved, (int, float)):
            self.model.timeout = max(1.0, min(saved, seconds))
        try:
            reply = self.model.complete(messages, tools)
        finally:
            if seconds is not None and isinstance(saved, (int, float)):
                self.model.timeout = saved
        self.ledger.record(arm=arm, role=role, question=question, attempt=attempt, model=self.model.name,
                           reply=reply, price=self.price, bound=bound)
        return reply


def read_key_file(variable: str, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The API key in the file whose path the environment variable `variable` holds, or None
    when the variable is unset. The key is never logged; a problem names only the variable."""
    env = os.environ if environ is None else environ
    where = env.get(variable)
    if not where:
        return None
    try:
        key = Path(where).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SpendError(f"cannot read the key file named by ${variable} ({type(exc).__name__})") from None
    if not key:
        raise SpendError(f"the key file named by ${variable} is empty")
    return key


# --- the sandbox -----------------------------------------------------------------------------


class SandboxError(RuntimeError):
    """The bash arm cannot run here."""


TOOLS = ("rg", "jq", "python3")
#: Where tools are found inside the sandbox without binding anything: /usr is mounted whole.
_SYSTEM_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")
_EXTRA_TOOLS = "/opt/tools"
#: The agent's HOME inside the sandbox: its own tmpfs, empty for every command.
SANDBOX_HOME = "/agent-home"


def find_bwrap() -> Optional[str]:
    return shutil.which("bwrap")


@dataclass
class BashResult:
    text: str
    exit_code: Optional[int]
    timed_out: bool = False
    cut: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.cut


class BashSandbox:
    """`bash -c` in bubblewrap: no network, an empty environment, the corpus read-only at /corpus."""

    def __init__(self, corpus: Path, *, bwrap: Optional[str] = None, command_seconds: float = 120.0,
                 output_bytes: int = 64 * 1024) -> None:
        self.bwrap = bwrap or find_bwrap()
        if not self.bwrap:
            raise SandboxError("bubblewrap (bwrap) is not installed on this host; the agent-bash arm refuses "
                               "to run without its sandbox")
        try:
            self.corpus = Path(corpus).resolve(strict=True)
        except OSError as exc:
            raise SandboxError(f"the corpus directory {corpus} does not exist") from exc
        self.command_seconds = command_seconds
        self.output_bytes = output_bytes
        self.binds: list[tuple[str, str]] = []
        self.tools: list[str] = []
        for name in TOOLS:
            if any(Path(d, name).is_file() for d in _SYSTEM_DIRS):
                self.tools.append(name)
                continue
            found = shutil.which(name)
            if found and name != "python3":  # a single binary can be bound; a python needs its tree
                self.binds.append((str(Path(found).resolve()), f"{_EXTRA_TOOLS}/{name}"))
                self.tools.append(name)
        shell = next((p for p in ("/usr/bin/bash", "/bin/bash") if Path(p).is_file()), None)
        if shell is None:
            raise SandboxError("no bash at /usr/bin/bash or /bin/bash")
        self.shell = shell

    def argv(self, command: str) -> list[str]:
        args = [
            self.bwrap,
            "--unshare-all", "--unshare-net", "--die-with-parent", "--new-session",
            "--clearenv",
            "--setenv", "PATH", f"/usr/local/bin:/usr/bin:/bin:{_EXTRA_TOOLS}",
            "--setenv", "HOME", SANDBOX_HOME,
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
            "--ro-bind", "/usr", "/usr",
        ]
        for top in ("/bin", "/sbin", "/lib", "/lib64", "/lib32"):
            path = Path(top)
            if path.is_symlink():
                args += ["--symlink", os.readlink(top), top]
            elif path.is_dir():
                args += ["--ro-bind", top, top]
        args += ["--ro-bind-try", "/etc/alternatives", "/etc/alternatives",
                 "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
                 "--proc", "/proc", "--dev", "/dev",
                 "--tmpfs", "/tmp", "--tmpfs", SANDBOX_HOME]
        for source, target in self.binds:
            args += ["--ro-bind", source, target]
        args += ["--ro-bind", str(self.corpus), "/corpus", "--chdir", "/corpus",
                 "--", self.shell, "--noprofile", "--norc", "-c", command]
        return args

    def run(self, command: str, *, seconds: Optional[float] = None) -> BashResult:
        limit = self.command_seconds if seconds is None else max(0.1, min(seconds, self.command_seconds))
        proc = subprocess.Popen(self.argv(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env={}, start_new_session=True)
        chunks: list[bytes] = []
        state = {"size": 0, "cut": False}

        def drain() -> None:
            assert proc.stdout is not None
            while True:
                block = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
                if not block:
                    return
                room = self.output_bytes - state["size"]
                if room > 0:
                    chunks.append(block[:room])
                state["size"] += len(block)
                if state["size"] > self.output_bytes and not state["cut"]:
                    state["cut"] = True
                    _kill(proc)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            proc.wait()
        reader.join(timeout=5)
        text = b"".join(chunks).decode("utf-8", "replace")
        notes = []
        if state["cut"]:
            notes.append(f"[output cut at {self.output_bytes} bytes; the command was stopped]")
        if timed_out:
            notes.append(f"[the command was stopped after {limit:g} seconds]")
        elif proc.returncode and not state["cut"]:
            notes.append(f"[exit code {proc.returncode}]")
        if notes:
            text = text + ("\n" if text and not text.endswith("\n") else "") + "\n".join(notes)
        return BashResult(text, None if timed_out else proc.returncode, timed_out, state["cut"])


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


BASH_SPEC = ToolSpec(
    "bash",
    "Run one bash command in the sandbox and return what it printed (stdout and stderr together). "
    "The working directory is /corpus.",
    {"type": "object", "properties": {"command": {"type": "string", "description": "the command to run"}},
     "required": ["command"]},
)


class BashTools:
    """The bash arm's tool box (the `ToolBox` shape the MCP one has)."""

    instructions: Optional[str] = None

    def __init__(self, sandbox: BashSandbox) -> None:
        self.sandbox = sandbox

    async def list_tools(self) -> list[ToolSpec]:
        return [BASH_SPEC]

    async def call(self, name: str, arguments: Mapping[str, Any], *, seconds: Optional[float] = None
                   ) -> tuple[str, bool]:
        command = arguments.get("command") if isinstance(arguments, Mapping) else None
        if name != "bash" or not isinstance(command, str) or not command.strip():
            return "error: call bash with {\"command\": \"...\"}", True
        result = await asyncio.to_thread(self.sandbox.run, command, seconds=seconds)
        return result.text, not result.ok


# --- one try ---------------------------------------------------------------------------------

ANSWERED, CAPPED, ERROR, STOPPED = "answered", "capped", "error", "stopped_at_cap"


@dataclass
class ToolEvent:
    name: str
    arguments: Mapping[str, Any]
    #: the response as the model saw it (at most `Caps.output_bytes`)
    text: str
    ok: bool


@dataclass
class TryResult:
    arm: str
    question: str
    attempt: int
    status: str = ANSWERED
    reason: Optional[str] = None
    answer: str = ""
    events: list[ToolEvent] = field(default_factory=list)
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    seconds: float = 0.0
    evidence: Optional["Evidence"] = None
    judge: Optional[dict[str, Any]] = None

    @property
    def tool_calls(self) -> int:
        return len(self.events)

    @property
    def scored(self) -> bool:
        """A try stopped by the spend cap did not complete; it is listed, not scored."""
        return self.status != STOPPED and self.evidence is not None and self.evidence.available > 0

    def to_dict(self) -> dict[str, Any]:
        out = {"try": self.attempt, "status": self.status, "reason": self.reason, "turns": self.turns,
               "tool_calls": self.tool_calls, "failed_tool_calls": sum(not e.ok for e in self.events),
               "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
               "cost_usd": round(self.cost, 6), "seconds": round(self.seconds, 1), "answer": self.answer,
               "calls": [{"tool": e.name, "arguments": dict(e.arguments), "ok": e.ok, "bytes": len(e.text)}
                         for e in self.events]}
        if self.evidence is not None:
            out["evidence"] = self.evidence.to_dict()
        if self.judge is not None:
            out["judge"] = self.judge
        return out


def _cut(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore") + f"\n[cut at {limit} bytes]"


async def run_try(model: Metered, tools: Any, specs: list[ToolSpec], *, arm: str, system: str, question_id: str,
                  question: str, attempt: int, caps: Caps, clock: Callable[[], float] = time.monotonic) -> TryResult:
    """One question, once, through the loop: model, tool calls, results back, until it answers or
    a cap ends it. A model failure ends it as an error; the spend cap ends it as stopped."""
    result = TryResult(arm, question_id, attempt)
    started = clock()
    deadline = started + caps.try_seconds
    schema = openai_tools(specs)
    known = {s.name for s in specs}
    messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    spent_before = model.ledger.spent

    def cap(reason: str) -> None:
        result.status, result.reason = CAPPED, reason

    try:
        while True:
            if result.turns >= caps.max_turns:
                cap(f"{caps.max_turns} model turns")
                break
            if clock() >= deadline:
                cap(f"{caps.try_seconds:g} seconds")
                break
            reply = await asyncio.to_thread(model.complete, messages, schema, arm=arm, role="agent",
                                            question=question_id, attempt=attempt, seconds=deadline - clock())
            result.turns += 1
            result.prompt_tokens += reply.prompt_tokens
            result.completion_tokens += reply.completion_tokens
            if not reply.tool_calls:
                result.answer = reply.content
                break
            messages.append({
                "role": "assistant", "content": reply.content,
                "tool_calls": [{"id": c.id, "type": "function",
                                "function": {"name": c.name, "arguments": json.dumps(dict(c.arguments))}}
                               for c in reply.tool_calls],
            })
            for call in reply.tool_calls:
                if result.tool_calls >= caps.max_tool_calls:
                    cap(f"{caps.max_tool_calls} tool calls")
                    break
                remaining = deadline - clock()
                if remaining <= 0:
                    cap(f"{caps.try_seconds:g} seconds")
                    break
                if call.name not in known:
                    text, is_error = f"error: no tool named {call.name!r}; tools are {sorted(known)}", True
                else:
                    try:
                        if isinstance(tools, BashTools):
                            text, is_error = await tools.call(call.name, call.arguments, seconds=remaining)
                        else:
                            text, is_error = await asyncio.wait_for(tools.call(call.name, call.arguments),
                                                                    timeout=remaining)
                    except asyncio.TimeoutError:
                        text, is_error = f"error: the call did not finish in the {remaining:.0f} s the try had left", True
                    except Exception as exc:  # noqa: BLE001 - a failing tool is data for the model
                        text, is_error = f"error: {_describe(exc)}", True
                shown = _cut(text, caps.output_bytes)
                result.events.append(ToolEvent(call.name, dict(call.arguments), shown, not is_error))
                messages.append({"role": "tool", "tool_call_id": call.id, "content": shown})
            if result.status == CAPPED:
                break
    except CapReached as exc:
        result.status, result.reason = STOPPED, str(exc)
    except ModelError as exc:
        result.status, result.reason = ERROR, str(exc)
    result.cost = model.ledger.spent - spent_before
    result.seconds = clock() - started
    return result


# --- gold records ----------------------------------------------------------------------------

RESOLVED, OVERRIDE, UNAVAILABLE = "resolved", "override", "unavailable"


@dataclass(frozen=True)
class GoldRecord:
    node_id: str
    status: str
    source: Optional[str] = None
    key: Optional[str] = None
    path: Optional[str] = None
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.status != UNAVAILABLE

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "source": self.source, "key": self.key, "path": self.path,
                "reason": self.reason or None}


class GoldError(RuntimeError):
    """Gold ids that did not resolve: the run refuses to score."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("the agent arms refuse to run: " + str(len(problems)) + " gold id(s) do not resolve to a "
                         "corpus record (a gold id that does not resolve is never scored as a miss):\n"
                         + "\n".join(f"  - {p}" for p in problems))
        self.problems = problems


def _find_node(payload: Any, node_id: str) -> Optional[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        if payload.get("node_id") == node_id:
            return payload
        for value in payload.values():
            found = _find_node(value, node_id)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_node(value, node_id)
            if found is not None:
                return found
    return None


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    subtype: str
    source: str
    attrs: Mapping[str, Any]


async def inspect_node(tools: Any, node_id: str, *, generation: Optional[str] = None) -> tuple[Optional[NodeInfo], str]:
    """(the node, "") or (None, why not), through the MCP inspect tool."""
    arguments: dict[str, Any] = {"target": {"kind": "node", "node_id": node_id}}
    if generation is not None:
        arguments["generation_id"] = generation
    try:
        text, is_error = await tools.call(INSPECT, arguments, meta=JSON_META)
    except Exception as exc:  # noqa: BLE001
        return None, f"inspect failed: {_describe(exc)}"
    if is_error:
        return None, f"inspect reported an error: {text.strip()[:200]}"
    try:
        payload = json.loads(text)
    except ValueError:
        return None, f"inspect did not answer in JSON: {text.strip()[:200]}"
    node = _find_node(payload, node_id)
    if node is None:
        return None, f"inspect did not return the node: {text.strip()[:200]}"
    provenance = node.get("provenance")
    source = (provenance.get("source") if isinstance(provenance, Mapping) else None) or node.get("source")
    if not isinstance(source, str) or not source:
        return None, "inspect returned the node without its source"
    attrs = node.get("attrs") if isinstance(node.get("attrs"), Mapping) else {}
    return NodeInfo(node_id, str(node.get("subtype") or ""), source, attrs), ""


def key_candidates(info: NodeInfo) -> list[str]:
    """Names a node may be filed under in keys.json, most specific first: its id, the id's
    tails, then its string attributes."""
    nid = info.node_id
    out = [nid]
    if ":" in nid:
        first = nid.split(":", 1)[1]
        out += [first, first.replace(":", "/"), nid.rsplit(":", 1)[1]]
    for name in ("url", "name", "key", "label"):
        value = info.attrs.get(name)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
    for value in info.attrs.values():
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            out.append(str(value).strip())
    return list(dict.fromkeys(out))


def source_candidates(source: str) -> list[str]:
    out = [source]
    if "." in source:
        out += [source.split(".", 1)[1], source.rsplit(".", 1)[1]]
    return list(dict.fromkeys(out))


class GoldMap:
    """Every scored question's gold ids, resolved to corpus records or marked unavailable."""

    def __init__(self, records: dict[str, list[GoldRecord]]) -> None:
        self.records = records

    def to_dict(self) -> dict[str, Any]:
        return {qid: {g.node_id: g.to_dict() for g in golds} for qid, golds in self.records.items()}


def _lookup(keys: Mapping[str, Any], source: str, names: Sequence[str]) -> tuple[Optional[str], Optional[str]]:
    table = keys["sources"][source]
    for name in names:
        path = table["lookup"].get(name)
        if path:
            return path, table["natural_key"].get(path, name)
    return None, None


def _left_out(keys: Mapping[str, Any], names: Sequence[str]) -> Optional[str]:
    left = keys.get("left_out") or {}
    for name in names:
        for variant in (name, name.replace("-", "_"), name.replace("_", "-")):
            if variant in left:
                return f"{variant}: {left[variant]}"
    return None


def record_for(info: NodeInfo, keys: Mapping[str, Any]) -> "GoldRecord | str":
    """The corpus record of an inspected node, an unavailable record (its source is left out of
    the corpus), or the reason it has none."""
    from archi.evaluation.raw_corpus import OPS_RECORD, OPS_RECORD_PRODUCER

    sources = keys.get("sources") or {}
    gid = info.node_id
    if info.source == OPS_RECORD_PRODUCER or info.subtype.startswith("ops_"):
        return GoldRecord(gid, UNAVAILABLE, OPS_RECORD, reason=_left_out(keys, [OPS_RECORD]) or "ops-record is left out")
    names = source_candidates(info.source)
    source = next((n for n in names if n in sources), None)
    if source is None:
        left = _left_out(keys, names)
        if left:
            return GoldRecord(gid, UNAVAILABLE, info.source, reason=left)
        return (f"its source {info.source} is neither in the corpus nor listed as left out; rebuild the "
                "corpus with `archi eval --raw-corpus`")
    candidates = key_candidates(info)
    path, natural = _lookup(keys, source, candidates)
    if path is None:
        return (f"no record of {source} in the corpus has its key (tried {', '.join(candidates[:4])}); "
                "the corpus and the graph differ, or add gold_sources for it")
    return GoldRecord(gid, RESOLVED, source, natural, path)


async def resolve_gold(tools: Any, questions: Sequence[Any], keys: Mapping[str, Any],
                       manifest: frozenset[str]) -> GoldMap:
    """Resolve every gold id, or raise GoldError naming each one that does not resolve."""
    sources = keys.get("sources") or {}
    out: dict[str, list[GoldRecord]] = {}
    problems: list[str] = []
    for question in questions:
        golds = []
        for gid in question.gold:
            where = (question.gold_sources or {}).get(gid)
            if where is not None:
                if "path" in where:
                    if where["path"] not in manifest:
                        problems.append(f"{question.id}: {gid}: gold_sources path {where['path']} is not in the corpus")
                        continue
                    natural = next((t["natural_key"].get(where["path"]) for name, t in sources.items()
                                    if where["path"] in t["natural_key"]), None)
                    owner = next((name for name, t in sources.items() if where["path"] in t["natural_key"]), None)
                    golds.append(GoldRecord(gid, OVERRIDE, owner, natural, where["path"]))
                    continue
                if where["source"] not in sources:
                    left = _left_out(keys, [where["source"]])
                    if left:
                        golds.append(GoldRecord(gid, UNAVAILABLE, where["source"], where["key"], reason=left))
                    else:
                        problems.append(f"{question.id}: {gid}: gold_sources source {where['source']} is not in the corpus")
                    continue
                path, natural = _lookup(keys, where["source"], [where["key"]])
                if path is None:
                    problems.append(f"{question.id}: {gid}: gold_sources key {where['key']!r} is not a record of "
                                    f"{where['source']} in the corpus")
                    continue
                golds.append(GoldRecord(gid, OVERRIDE, where["source"], natural, path))
                continue
            info, why = await inspect_node(tools, gid)
            if info is None:
                problems.append(f"{question.id}: {gid}: {why}")
                continue
            record = record_for(info, keys)
            if isinstance(record, str):
                problems.append(f"{question.id}: {gid}: {record}")
                continue
            golds.append(record)
        out[question.id] = golds
    if problems:
        raise GoldError(problems)
    return GoldMap(out)


# --- scoring ---------------------------------------------------------------------------------

#: A natural key shorter than this counts only through its path or node id: "gpu" or "12"
#: would match too much text.
MIN_KEY_CHARS = 4
_WORD = r"A-Za-z0-9_"
_PATH_TOKEN = re.compile(r"[\w.%@+=,~-]+(?:/[\w.%@+=,~-]+)+")
#: okg row heads (`[n] <id> (subtype)`) and JSON `"node_id": "<id>"` fields
_ROW_HEAD = re.compile(r"^\[\d+\] (\S+) \(", re.M)
_JSON_ID = re.compile(r'"node_id"\s*:\s*"([^"]+)"')


def _bounded(term: str, *, left: str, right: str) -> re.Pattern:
    return re.compile(rf"(?<![{left}]){re.escape(term)}(?![{right}])")


@dataclass
class Evidence:
    found: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    first_rank: Optional[int] = None
    records_read: int = 0

    @property
    def available(self) -> int:
        return len(self.found) + len(self.missed)

    @property
    def recall(self) -> Optional[float]:
        return len(self.found) / self.available if self.available else None

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.first_rank if self.first_rank else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"recall": None if self.recall is None else round(self.recall, 4),
                "reciprocal_rank": round(self.reciprocal_rank, 4), "first_rank": self.first_rank,
                "found": self.found, "missed": self.missed, "unavailable": self.unavailable,
                "records_read": self.records_read}


def _gold_patterns(gold: GoldRecord) -> list[re.Pattern]:
    patterns = [_bounded(gold.node_id, left=_WORD + ":", right=_WORD + "-")]
    if gold.path:
        patterns.append(_bounded(gold.path, left=_WORD + ".%-", right=_WORD + "%-"))
    if gold.key and len(gold.key) >= MIN_KEY_CHARS:
        patterns.append(_bounded(gold.key, left=_WORD, right=_WORD))
    return patterns


def score_evidence(events: Sequence[ToolEvent], gold: Sequence[GoldRecord], manifest: frozenset[str]) -> Evidence:
    """Evidence recall and the first gold record's rank, from successful tool responses only.

    Tool inputs (`ToolEvent.arguments`) and the final answer are never read."""
    evidence = Evidence(unavailable=[g.node_id for g in gold if not g.available])
    targets = [g for g in gold if g.available]
    patterns = {g.node_id: _gold_patterns(g) for g in targets}
    alias = {}
    for g in targets:
        alias[g.node_id] = g.node_id
        if g.path:
            alias[g.path] = g.node_id
    found: set[str] = set()
    order: list[str] = []
    for event in events:
        if not event.ok:
            continue
        mentions: list[tuple[int, str]] = []
        for g in targets:
            for pattern in patterns[g.node_id]:
                for match in pattern.finditer(event.text):
                    mentions.append((match.start(), g.node_id))
        for match in _PATH_TOKEN.finditer(event.text):
            token = match.group(0)
            for prefix in ("./", "corpus/"):
                if token.startswith(prefix):
                    token = token[len(prefix):]
            if token in manifest:
                mentions.append((match.start(), alias.get(token, "path:" + token)))
        for regex in (_ROW_HEAD, _JSON_ID):
            for match in regex.finditer(event.text):
                mentions.append((match.start(1), alias.get(match.group(1), "id:" + match.group(1))))
        for _, identity in sorted(mentions):
            if identity not in order:
                order.append(identity)
            if identity in patterns:
                found.add(identity)
    evidence.found = [g.node_id for g in targets if g.node_id in found]
    evidence.missed = [g.node_id for g in targets if g.node_id not in found]
    evidence.records_read = len(order)
    evidence.first_rank = next((i for i, name in enumerate(order, 1) if name in patterns), None)
    return evidence


# --- the judge -------------------------------------------------------------------------------

JUDGE_PROMPT = """\
You grade one answer to an operations question against a reference answer an operator wrote.

Score 1 when the answer states what the reference states (wording may differ). Score 0.5 when \
it states part of it, or hedges between the reference and something else. Score 0 when it is \
wrong, says it could not find the answer, or misses the point. Ignore the "Sources:" line and \
the style.

Reply with JSON only: {"score": 0 or 0.5 or 1, "reason": "one sentence"}"""


@dataclass
class Judge:
    model: Metered
    passes: int = 1

    def grade(self, *, arm: str, question_id: str, attempt: int, question: str, key: str, answer: str
              ) -> dict[str, Any]:
        """{"score": mean of the passes that parsed, or None; "passes": [...]}. An empty answer is
        a 0 without a call."""
        if not answer.strip():
            return {"score": 0.0, "passes": [], "note": "no answer"}
        messages = [{"role": "system", "content": JUDGE_PROMPT},
                    {"role": "user", "content": f"Question: {question}\n\nReference answer: {key}\n\n"
                                                f"Answer to grade:\n{answer}"}]
        passes = []
        for _ in range(self.passes):
            reply = self.model.complete(messages, [], arm=arm, role="judge", question=question_id, attempt=attempt)
            passes.append(_verdict(reply.content))
        scores = [p["score"] for p in passes if p.get("score") is not None]
        return {"score": sum(scores) / len(scores) if scores else None, "passes": passes}


def _verdict(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else None
    except ValueError:
        data = None
    score = data.get("score") if isinstance(data, Mapping) else None
    if isinstance(score, bool) or score not in (0, 0.5, 1):
        return {"score": None, "error": f"unreadable verdict: {(text or '')[:200]}"}
    return {"score": float(score), "reason": str(data.get("reason") or "")[:300]}


# --- the run ---------------------------------------------------------------------------------


@dataclass
class AgentPlan:
    arms: tuple[str, ...]
    model: Metered
    caps: Caps
    tries: int
    corpus: Any  # raw_corpus.Corpus, verified
    sandbox: Optional[BashSandbox] = None
    judge: Optional[Judge] = None
    judge_requested: bool = False
    reasoning_effort: Optional[str] = None
    model_url: str = ""


@dataclass
class AgentReport:
    plan: AgentPlan
    gold: GoldMap
    tries: dict[str, dict[str, list[TryResult]]] = field(default_factory=dict)  # question -> arm -> tries
    planned: int = 0
    stopped: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""

    @property
    def answer_keys(self) -> int:
        return sum(1 for q in self.questions if q.answer)

    questions: list = field(default_factory=list)

    def summary(self, arm: str) -> dict[str, Any]:
        rows = [t for per_arm in self.tries.values() for t in per_arm.get(arm, [])]
        scored = [t for t in rows if t.scored]
        recalls = [t.evidence.recall for t in scored]
        rrs = [t.evidence.reciprocal_rank for t in scored]
        completed = [t for t in rows if t.status != STOPPED]
        if not self.answer_keys:
            answer: Any = NO_ANSWER_KEYS
        elif self.plan.judge is None:
            answer = JUDGE_OFF
        else:
            graded = [t.judge["score"] for t in completed if t.judge and t.judge.get("score") is not None]
            answer = round(sum(graded) / len(graded), 4) if graded else None
        return {
            "tries": len(rows),
            "tries_scored": len(scored),
            "evidence_recall": round(sum(recalls) / len(recalls), 4) if recalls else None,
            "mrr": round(sum(rrs) / len(rrs), 4) if rrs else None,
            "answer_score": answer,
            "judge_errors": sum(1 for t in completed if t.judge and t.judge.get("score") is None),
            "cost_usd": round(sum(t.cost for t in rows), 6),
            "cost_per_try_usd": round(sum(t.cost for t in completed) / len(completed), 6) if completed else None,
            "tool_calls_per_try": round(sum(t.tool_calls for t in completed) / len(completed), 2) if completed else None,
            "turns_per_try": round(sum(t.turns for t in completed) / len(completed), 2) if completed else None,
            "failures": sum(t.status == ERROR for t in rows),
            "capped": sum(t.status == CAPPED for t in rows),
            "stopped_at_cap": sum(t.status == STOPPED for t in rows),
        }

    def to_dict(self) -> dict[str, Any]:
        plan = self.plan
        ledger = plan.model.ledger
        judge_cost = 0.0
        if plan.judge is not None:
            judge_cost = ledger.spent - sum(t.cost for per in self.tries.values() for ts in per.values() for t in ts)
        return {
            "arms": {arm: self.summary(arm) for arm in plan.arms},
            "model": plan.model.model.name,
            "model_url": plan.model_url,
            "reasoning_effort": plan.reasoning_effort,
            "price_usd_per_million": vars(plan.model.price),
            "caps": {"max_turns": plan.caps.max_turns, "max_tool_calls": plan.caps.max_tool_calls,
                     "try_seconds": plan.caps.try_seconds, "command_seconds": plan.caps.command_seconds,
                     "output_bytes": plan.caps.output_bytes, "max_output_tokens": plan.model.max_output_tokens},
            "tries_per_question": plan.tries,
            "prompt": {arm: build_prompt(arm, plan.caps, folders=_folders(plan.corpus),
                                         tools=plan.sandbox.tools if plan.sandbox else ()) for arm in plan.arms},
            "sandbox": None if plan.sandbox is None else {"bwrap": plan.sandbox.bwrap, "tools": plan.sandbox.tools,
                                                         "missing_tools": [t for t in TOOLS if t not in plan.sandbox.tools]},
            "corpus": {"path": str(plan.corpus.path), "digest": plan.corpus.digest,
                       "lock_sha256": plan.corpus.lock_sha256, "built_at": plan.corpus.built_at,
                       "sources": sorted(plan.corpus.sources), "left_out": dict(plan.corpus.left_out)},
            "ops_record": "left out of both agent arms: " + str(plan.corpus.left_out.get("ops-record", "")),
            "judge": {"requested": plan.judge_requested, "enabled": plan.judge is not None,
                      "model": plan.judge.model.model.name if plan.judge else None,
                      "passes": plan.judge.passes if plan.judge else None,
                      "answer_keys": self.answer_keys, "cost_usd": round(judge_cost, 6)},
            "spend": {"cap_usd": ledger.cap, "spent_usd": round(ledger.spent, 6), "calls": ledger.calls,
                      "stopped_at_cap": self.stopped is not None, "stop_reason": self.stopped,
                      "ledger": LEDGER},
            "tries_planned": self.planned,
            "tries_run": sum(len(ts) for per in self.tries.values() for ts in per.values()),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "gold": self.gold.to_dict(),
            "questions": [{"id": qid, "arms": {arm: [t.to_dict() for t in ts] for arm, ts in per.items()}}
                          for qid, per in self.tries.items()],
        }


def _folders(corpus: Any) -> list[str]:
    try:
        return sorted(p.name for p in corpus.records.iterdir() if p.is_dir())
    except OSError:
        return []


async def run_agent_arms(questions: Sequence[Any], plan: AgentPlan, mcp: Any, gold: GoldMap, *,
                         manifest: frozenset[str],
                         progress: Optional[Callable[[TryResult, int, int], None]] = None) -> AgentReport:
    """Every scored question, `plan.tries` times, in each arm (arms interleaved per question so a
    spend stop leaves both arms with the same questions done)."""
    report = AgentReport(plan, gold, questions=list(questions), started_at=_now())
    folders = _folders(plan.corpus)
    systems = {arm: build_prompt(arm, plan.caps, folders=folders,
                                 tools=plan.sandbox.tools if plan.sandbox else ()) for arm in plan.arms}
    boxes: dict[str, Any] = {}
    specs: dict[str, list[ToolSpec]] = {}
    for arm in plan.arms:
        if arm == AGENT_BASH:
            if plan.sandbox is None:
                raise SandboxError("the agent-bash arm needs its sandbox")
            boxes[arm] = BashTools(plan.sandbox)
        else:
            boxes[arm] = mcp
        specs[arm] = await boxes[arm].list_tools()
    todo = [q for q in questions if any(g.available for g in gold.records.get(q.id, []))]
    report.planned = len(todo) * plan.tries * len(plan.arms)
    done = 0
    for question in todo:
        per_arm = report.tries.setdefault(question.id, {})
        for attempt in range(1, plan.tries + 1):
            for arm in plan.arms:
                if plan.model.ledger.stopped:
                    report.stopped = plan.model.ledger.stopped
                    break
                result = await run_try(plan.model, boxes[arm], specs[arm], arm=arm, system=systems[arm],
                                       question_id=question.id, question=question.question, attempt=attempt,
                                       caps=plan.caps)
                result.evidence = score_evidence(result.events, gold.records[question.id], manifest)
                if plan.judge is not None and question.answer and result.status != STOPPED:
                    try:
                        result.judge = await asyncio.to_thread(
                            plan.judge.grade, arm=arm, question_id=question.id, attempt=attempt,
                            question=question.question, key=question.answer, answer=result.answer)
                    except CapReached as exc:
                        result.judge = {"score": None, "error": f"stopped at the spend cap: {exc}"}
                    except ModelError as exc:
                        result.judge = {"score": None, "error": str(exc)}
                per_arm.setdefault(arm, []).append(result)
                done += 1
                if progress:
                    progress(result, done, report.planned)
            if report.stopped:
                break
        if report.stopped:
            break
    if plan.model.ledger.stopped and report.stopped is None:
        report.stopped = plan.model.ledger.stopped
    report.finished_at = _now()
    return report


# --- the report section ----------------------------------------------------------------------


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "none"
    if isinstance(value, str):
        return value
    return f"{value:.{digits}f}"


def render_agent_markdown(agents: Mapping[str, Any], data: Mapping[str, Any]) -> list[str]:
    """The agent arms beside the no-model arms, for report.md."""
    k = data.get("k")
    lines = ["## All arms side by side", "",
             "| arm | what it is | evidence recall | MRR | answer score | cost per try | tool calls per try | failures |",
             "|---|---|---|---|---|---|---|---|"]
    for name, s in (data.get("arms") or {}).items():
        lines.append(f"| {name} | search call, no model | {_fmt(s['recall_at_k'])} (recall@{k}) | {_fmt(s['mrr'])} "
                     f"| n/a | $0 | 1 | {s['errors']} failed calls |")
    what = {AGENT_BASH: "agent, bash over the raw files", AGENT_GRAPH: "agent, the instance's MCP tools"}
    for name, s in agents["arms"].items():
        cost = "none" if s["cost_per_try_usd"] is None else f"${s['cost_per_try_usd']:.4f}"
        failures = f"{s['failures']} errors, {s['capped']} capped" + (
            f", {s['stopped_at_cap']} stopped at the spend cap" if s["stopped_at_cap"] else "")
        lines.append(f"| {name} | {what.get(name, 'agent')} | {_fmt(s['evidence_recall'])} | {_fmt(s['mrr'])} "
                     f"| {_fmt(s['answer_score'])} | {cost} | {_fmt(s['tool_calls_per_try'], 1)} | {failures} |")
    spend = agents["spend"]
    corpus = agents["corpus"]
    caps = agents["caps"]
    lines += [
        "",
        f"- agent arms: model {agents['model']} (reasoning {agents['reasoning_effort'] or 'default'}), "
        f"{agents['tries_per_question']} tries per question; caps {caps['max_turns']} turns, "
        f"{caps['max_tool_calls']} tool calls, {caps['try_seconds']:g} s per try; bash commands "
        f"{caps['command_seconds']:g} s and {caps['output_bytes'] // 1024} KiB",
        f"- corpus: `{corpus['digest']}` at {corpus['path']} (snapshot lock `{corpus['lock_sha256'][:16]}`); "
        f"sources {', '.join(corpus['sources'])}",
        f"- left out of the corpus: " + "; ".join(f"{k}: {v}" for k, v in corpus["left_out"].items()),
        f"- {agents['ops_record']}",
        f"- spend: ${spend['spent_usd']:.4f} of the ${spend['cap_usd']:.2f} cap over {spend['calls']} model calls "
        f"(ledger `{spend['ledger']}`)" + (f"; **stopped at the cap**: {spend['stop_reason']}; "
                                           f"{agents['tries_run']} of {agents['tries_planned']} tries ran"
                                           if spend["stopped_at_cap"] else ""),
        f"- judge: " + (NO_ANSWER_KEYS if not agents["judge"]["answer_keys"] else
                        (f"{agents['judge']['model']}, {agents['judge']['passes']} pass(es), "
                         f"{agents['judge']['answer_keys']} answer keys" if agents["judge"]["enabled"] else JUDGE_OFF)),
    ]
    if agents.get("sandbox") and agents["sandbox"]["missing_tools"]:
        lines.append(f"- not installed on this host, so not in the sandbox: {', '.join(agents['sandbox']['missing_tools'])}")
    unavailable = sorted({gid for per in agents["gold"].values() for gid, g in per.items() if g["status"] == UNAVAILABLE})
    if unavailable:
        lines.append(f"- gold records unavailable to the agent arms (not counted as misses): {', '.join(unavailable)}")
    return lines
