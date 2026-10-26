"""Run a smoke set and write report.md and report.json."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from archi.evaluation.agent import (
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_TOOL_CHARS,
    ChatModel,
    ToolBox,
    ToolError,
    ToolSpec,
    ask,
    build_system_prompt,
    prompt_hash,
    sha,
)
from archi.evaluation.smoke import NodeInfo, SmokeSet, check_answer, merge_nodes, nodes_in

REPORT_SCHEMA = "archi-eval-report/1"
PASS, FAIL, EXECUTION_FAILED = "pass", "fail", "execution_failed"

STOP_GAP_NOTE = (
    "This is the $0 smoke harness (PACT r3-bundle-evals). The eval framework tracked in "
    "#619 replaces it; the question files and this report format are kept so they can move across."
)


@dataclass
class QuestionResult:
    id: str
    question: str
    status: str
    reasons: list[str] = field(default_factory=list)
    cited: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    answer: str = ""
    error: Optional[str] = None


@dataclass
class Report:
    bundle: str
    model: str
    model_url: str
    mcp_url: str
    pass_threshold: int
    smoke_file: str
    tools: list[str] = field(default_factory=list)
    prompt_hash: str = ""
    #: what the model was given besides the question, each as a hash (None: not given)
    mcp_instructions_hash: Optional[str] = None
    chat_prompt_hash: Optional[str] = None
    chat_prompt_file: Optional[str] = None
    system_prompt_hash: Optional[str] = None
    questions: list[QuestionResult] = field(default_factory=list)
    started_at: str = ""
    seconds: float = 0.0
    error: Optional[str] = None

    @property
    def passed(self) -> int:
        return sum(q.status == PASS for q in self.questions)

    @property
    def status(self) -> str:
        """`execution_failed` when nothing could run, else `pass` or `fail` against the threshold."""
        if self.error or not self.questions or all(q.status == EXECUTION_FAILED for q in self.questions):
            return EXECUTION_FAILED
        return PASS if self.passed >= self.pass_threshold else FAIL

    @property
    def exit_code(self) -> int:
        return {PASS: 0, FAIL: 1, EXECUTION_FAILED: 2}[self.status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "note": STOP_GAP_NOTE,
            "bundle": self.bundle,
            "status": self.status,
            "passed": self.passed,
            "total": len(self.questions),
            "pass_threshold": self.pass_threshold,
            "model": self.model,
            "model_url": self.model_url,
            "mcp_url": self.mcp_url,
            "tools": self.tools,
            "prompt_hash": self.prompt_hash,
            "mcp_instructions_hash": self.mcp_instructions_hash,
            "chat_prompt_hash": self.chat_prompt_hash,
            "chat_prompt_file": self.chat_prompt_file,
            "system_prompt_hash": self.system_prompt_hash,
            "smoke_file": self.smoke_file,
            "started_at": self.started_at,
            "seconds": round(self.seconds, 1),
            "prompt_tokens": sum(q.prompt_tokens for q in self.questions),
            "completion_tokens": sum(q.completion_tokens for q in self.questions),
            "error": self.error,
            "questions": [
                {
                    "id": q.id,
                    "question": q.question,
                    "status": q.status,
                    "reasons": q.reasons,
                    "cited": q.cited,
                    "tool_calls": q.tool_calls,
                    "prompt_tokens": q.prompt_tokens,
                    "completion_tokens": q.completion_tokens,
                    "seconds": round(q.seconds, 1),
                    "answer": q.answer,
                    "error": q.error,
                }
                for q in self.questions
            ],
        }


ToolBoxFactory = Callable[[], Any]  # an async context manager yielding a ToolBox


async def run_smoke(
    smoke: SmokeSet,
    model: ChatModel,
    open_tools: ToolBoxFactory,
    *,
    model_url: str = "",
    mcp_url: str = "",
    chat_prompt: Optional[str] = None,
    chat_prompt_file: str = "",
    max_steps: int = DEFAULT_MAX_STEPS,
    max_tool_chars: int = DEFAULT_MAX_TOOL_CHARS,
    progress: Optional[Callable[[QuestionResult, int, int], None]] = None,
) -> Report:
    report = Report(
        bundle=smoke.bundle,
        model=model.name,
        model_url=model_url,
        mcp_url=mcp_url,
        pass_threshold=smoke.pass_threshold,
        smoke_file=str(smoke.path or ""),
        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    started = time.monotonic()
    try:
        async with open_tools() as tools:
            specs: list[ToolSpec] = await tools.list_tools()
            report.tools = sorted(s.name for s in specs)
            instructions = getattr(tools, "instructions", None)
            system = build_system_prompt(instructions, chat_prompt)
            report.prompt_hash = prompt_hash(specs, system)
            report.mcp_instructions_hash = sha(instructions)
            report.chat_prompt_hash = sha(chat_prompt)
            report.chat_prompt_file = chat_prompt_file or None
            report.system_prompt_hash = sha(system)
            if not specs:
                report.error = "the MCP endpoint offers no tools, so the agent has nothing to read with"
                report.questions = [
                    QuestionResult(q.id, q.question, EXECUTION_FAILED, error=report.error)
                    for q in smoke.questions
                ]
            else:
                for i, question in enumerate(smoke.questions, 1):
                    result = await _one(question, model, tools, specs, system, max_steps, max_tool_chars)
                    report.questions.append(result)
                    if progress:
                        progress(result, i, len(smoke.questions))
    except ToolError as exc:
        report.error = f"MCP endpoint not usable: {exc}"
        report.questions = [
            QuestionResult(q.id, q.question, EXECUTION_FAILED, error=report.error) for q in smoke.questions
        ]
    report.seconds = time.monotonic() - started
    return report


async def _one(question, model, tools: ToolBox, specs, system, max_steps, max_tool_chars) -> QuestionResult:
    trace = await ask(model, tools, specs, question.question, system=system, max_steps=max_steps,
                      max_tool_chars=max_tool_chars)
    result = QuestionResult(
        id=question.id,
        question=question.question,
        status=FAIL,
        tool_calls=[
            {"name": t.name, "arguments": dict(t.arguments), "chars": t.chars, "is_error": t.is_error}
            for t in trace.tool_calls
        ],
        prompt_tokens=trace.prompt_tokens,
        completion_tokens=trace.completion_tokens,
        seconds=trace.seconds,
        answer=trace.answer,
        error=trace.error,
    )
    if trace.error:
        result.status = EXECUTION_FAILED
        return result
    if not trace.finished:
        result.reasons = ["no answer: the step limit ran out while the model was still calling tools"]
        return result
    seen: dict[str, NodeInfo] = {}
    for text in trace.results:
        merge_nodes(seen, nodes_in(text))
    verdict = check_answer(question, trace.answer, seen)
    result.cited = [{"id": n.id, "subtype": n.subtype, "source": n.source} for n in verdict.cited]
    result.reasons = verdict.reasons
    result.status = PASS if verdict.passed else FAIL
    return result


# --- writing ---------------------------------------------------------------------------


def render_markdown(report: Report) -> str:
    data = report.to_dict()
    lines = [
        f"# archi eval: {report.bundle}",
        "",
        f"**{report.status}**: {report.passed} of {len(report.questions)} questions passed "
        f"(threshold {report.pass_threshold}).",
        "",
        f"> {STOP_GAP_NOTE}",
        "",
        f"- model: `{report.model}` at {report.model_url}",
        f"- MCP: {report.mcp_url}; tools: {', '.join(report.tools) or 'none'}",
        f"- prompt hash: `{report.prompt_hash or 'none'}`; system message `{report.system_prompt_hash}`",
        f"- MCP server instructions: `{report.mcp_instructions_hash}`; chat prompt: "
        f"`{report.chat_prompt_hash}` ({report.chat_prompt_file or 'none'})",
        f"- started {report.started_at}; wall time {data['seconds']} s; "
        f"tokens {data['prompt_tokens']} in / {data['completion_tokens']} out",
        f"- smoke file: {report.smoke_file}",
    ]
    if report.error:
        lines += ["", f"**Error:** {report.error}"]
    lines += ["", "| question | result | tool calls | cited | tokens in/out | seconds |", "|---|---|---|---|---|---|"]
    for q in report.questions:
        cited = ", ".join(f"`{c['id']}`" for c in q.cited[:3]) + (" ..." if len(q.cited) > 3 else "")
        lines.append(
            f"| {q.id} | {q.status} | {len(q.tool_calls)} | {cited or '-'} | "
            f"{q.prompt_tokens}/{q.completion_tokens} | {q.seconds:.1f} |"
        )
    for q in report.questions:
        lines += ["", f"## {q.id}: {q.status}", "", f"Q: {q.question}", ""]
        if q.reasons:
            lines += [f"- {r}" for r in q.reasons] + [""]
        if q.error:
            lines += [f"Error: {q.error}", ""]
        if q.tool_calls:
            lines.append("Tool calls: " + "; ".join(f"`{t['name']}({json.dumps(t['arguments'])})`" for t in q.tool_calls))
            lines.append("")
        if q.answer:
            lines += ["Answer:", "", *[f"> {ln}" for ln in q.answer.strip().splitlines()]]
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: Report, out: Path) -> tuple[Path, Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "report.md", out / "report.json"
    md.write_text(render_markdown(report), encoding="utf-8")
    js.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return md, js
