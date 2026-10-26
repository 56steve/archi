"""The agent loop, the run and the report, with a fake model and a fake MCP (no network)."""
import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager

import pytest

from archi.evaluation import agent as agent_mod
from archi.evaluation.agent import ChatReply, ModelError, ToolCall, ToolError, ToolSpec
from archi.evaluation.runner import EXECUTION_FAILED, FAIL, PASS, run_smoke, write_report
from archi.evaluation.smoke import parse_smoke

SEARCH = ToolSpec("search", "Search the graph", {"type": "object", "properties": {"query": {"type": "string"}}})
RESULT = (
    'search ok · "q" · source gp.docsite · rows 1-1\n\n'
    '[1] documentation_page:22 (documentation_page) · "Intro · FTS3"\n'
    "    FTS3 is the File Transfer Service.\n"
)


class FakeTools:
    def __init__(self, specs=(SEARCH,), text=RESULT, fail=False):
        self.specs, self.text, self.fail, self.calls = list(specs), text, fail, []

    async def list_tools(self):
        return self.specs

    async def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self.fail:
            raise RuntimeError("boom")
        return self.text, False


def opener(tools):
    @asynccontextmanager
    async def open_tools():
        yield tools

    return open_tools


class FakeModel:
    """Replies are scripted per question: a list of ChatReply, consumed in order."""

    name = "fake:1b"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

    def complete(self, messages, tools):
        self.seen.append((list(messages), tools))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def searches(query="FTS3"):
    return ChatReply("", (ToolCall("c1", "search", {"query": query}),), 10, 2)


def answers(text):
    return ChatReply(text, (), 20, 5)


def smoke_of(*ids, threshold=1):
    rows = [{"id": i, "question": f"question {i}?", "must_cite": {"sources": ["docsite"]}} for i in ids]
    return parse_smoke({"bundle": "demo", "pass_threshold": threshold, "questions": rows})


def run(smoke, model, tools, **kw):
    return asyncio.run(run_smoke(smoke, model, opener(tools), model_url="http://127.0.0.1:11434/v1",
                                 mcp_url="http://127.0.0.1:1/mcp", **kw))


def test_a_cited_answer_passes_and_the_trace_is_recorded():
    model = FakeModel(searches(), answers("FTS3 is a transfer service.\nSources: documentation_page:22"))
    report = run(smoke_of("a"), model, FakeTools())
    q = report.questions[0]
    assert (q.status, q.reasons) == (PASS, [])
    assert q.cited == [{"id": "documentation_page:22", "subtype": "documentation_page", "source": "gp.docsite"}]
    assert q.tool_calls == [{"name": "search", "arguments": {"query": "FTS3"}, "chars": len(RESULT), "is_error": False}]
    assert (q.prompt_tokens, q.completion_tokens) == (30, 7)
    assert report.status == PASS and report.exit_code == 0 and report.tools == ["search"]
    assert report.prompt_hash.startswith("sha256:")


def test_the_model_sees_the_system_prompt_the_question_and_the_tool_result():
    model = FakeModel(searches(), answers("done documentation_page:22"))
    run(smoke_of("a"), model, FakeTools())
    first, tools = model.seen[0]
    assert first[0]["role"] == "system" and first[1] == {"role": "user", "content": "question a?"}
    assert tools[0]["function"]["name"] == "search"
    second, _ = model.seen[1]
    assert second[-1]["role"] == "tool" and "FTS3 is the File Transfer Service." in second[-1]["content"]


def test_an_answer_with_no_citation_fails_not_errors():
    report = run(smoke_of("a"), FakeModel(searches(), answers("FTS3 is a transfer service.")), FakeTools())
    q = report.questions[0]
    assert q.status == FAIL and "cited no node from source docsite" in q.reasons[0]
    assert report.status == FAIL and report.exit_code == 1


def test_a_made_up_id_fails():
    report = run(smoke_of("a"), FakeModel(searches(), answers("Sources: documentation_page:7")), FakeTools())
    assert report.questions[0].status == FAIL


def test_an_answer_without_any_tool_call_cannot_cite():
    report = run(smoke_of("a"), FakeModel(answers("From memory. Sources: documentation_page:22")), FakeTools())
    assert report.questions[0].status == FAIL and report.questions[0].tool_calls == []


def test_zero_mcp_tools_is_execution_failed_for_the_whole_run():
    model = FakeModel()
    report = run(smoke_of("a", "b"), model, FakeTools(specs=()))
    assert [q.status for q in report.questions] == [EXECUTION_FAILED] * 2
    assert report.status == EXECUTION_FAILED and report.exit_code == 2
    assert "no tools" in report.error and model.seen == []


def test_an_unreachable_mcp_endpoint_is_execution_failed():
    @asynccontextmanager
    async def refused():
        raise ToolError("http://127.0.0.1:1/mcp: connection refused")
        yield  # pragma: no cover

    model = FakeModel()
    report = asyncio.run(run_smoke(smoke_of("a"), model, refused))
    assert report.status == EXECUTION_FAILED and "connection refused" in report.error


def test_a_model_error_is_execution_failed_for_that_question_only():
    model = FakeModel(ModelError("model not found"), searches(), answers("documentation_page:22"))
    report = run(smoke_of("a", "b", threshold=1), model, FakeTools())
    assert [q.status for q in report.questions] == [EXECUTION_FAILED, PASS]
    assert report.questions[0].error == "model not found"
    assert report.status == PASS


def test_every_question_erroring_makes_the_run_execution_failed():
    model = FakeModel(ModelError("down"), ModelError("down"))
    report = run(smoke_of("a", "b"), model, FakeTools())
    assert report.status == EXECUTION_FAILED and report.exit_code == 2


def test_the_threshold_decides_pass_or_fail():
    def result(threshold):
        model = FakeModel(searches(), answers("documentation_page:22"), searches(), answers("no cite"))
        return run(smoke_of("a", "b", threshold=threshold), model, FakeTools())

    assert result(1).status == PASS and result(1).passed == 1
    assert result(2).status == FAIL and result(2).exit_code == 1


def test_a_failing_tool_is_reported_to_the_model_not_raised():
    model = FakeModel(searches(), answers("could not read it"))
    report = run(smoke_of("a"), model, FakeTools(fail=True))
    assert report.questions[0].tool_calls[0]["is_error"] is True
    assert "error: RuntimeError: boom" in model.seen[1][0][-1]["content"]


def test_an_unknown_tool_name_is_an_error_the_model_can_see():
    bad = ChatReply("", (ToolCall("c1", "delete_everything", {}),))
    model = FakeModel(bad, answers("documentation_page:22"))
    tools = FakeTools()
    report = run(smoke_of("a"), model, tools)
    assert tools.calls == [] and report.questions[0].tool_calls[0]["is_error"]


def test_the_step_limit_ends_a_model_that_never_answers():
    loop = [searches() for _ in range(3)]
    report = run(smoke_of("a"), FakeModel(*loop), FakeTools(), max_steps=3)
    q = report.questions[0]
    assert q.status == FAIL and "step limit" in q.reasons[0] and len(q.tool_calls) == 3


def test_long_tool_output_is_cut_for_the_model_but_ids_come_from_what_it_saw():
    text = RESULT + "x" * 5000 + "\n[2] documentation_page:99 (documentation_page) · source gp.docsite\n"
    model = FakeModel(searches(), answers("documentation_page:99"))
    report = run(smoke_of("a"), model, FakeTools(text=text), max_tool_chars=200)
    assert report.questions[0].status == FAIL
    assert model.seen[1][0][-1]["content"].endswith("[cut]")


def test_the_report_is_written_as_markdown_and_json(tmp_path):
    model = FakeModel(searches(), answers("FTS3.\nSources: documentation_page:22"), searches(), answers("none"))
    report = run(smoke_of("a", "b", threshold=2), model, FakeTools())
    md, js = write_report(report, tmp_path / "out")
    data = json.loads(js.read_text())
    assert data["schema"] == "archi-eval-report/1"
    assert {k: data[k] for k in ("bundle", "status", "passed", "total", "pass_threshold", "model")} == {
        "bundle": "demo", "status": "fail", "passed": 1, "total": 2, "pass_threshold": 2, "model": "fake:1b"}
    assert data["tools"] == ["search"] and data["prompt_hash"] == report.prompt_hash
    assert "#619" in data["note"]
    first = data["questions"][0]
    assert set(first) == {"id", "question", "status", "reasons", "cited", "tool_calls", "prompt_tokens",
                          "completion_tokens", "seconds", "answer", "error"}
    assert first["cited"][0]["id"] == "documentation_page:22"
    text = md.read_text()
    assert "# archi eval: demo" in text and "**fail**: 1 of 2" in text and "| a | pass |" in text
    assert "#619" in text


def test_the_prompt_hash_changes_with_the_tools():
    one = agent_mod.prompt_hash([SEARCH])
    two = agent_mod.prompt_hash([SEARCH, ToolSpec("inspect", "Read", {})])
    assert one != two and one == agent_mod.prompt_hash([SEARCH])


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434/v1", "http://localhost:11500/v1", "http://ollama:11434/v1",
                                 "http://10.0.0.5:11434/v1", "http://192.168.1.2/v1"])
def test_local_model_endpoints_are_allowed(url):
    agent_mod.require_local(url)


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://api.anthropic.com/v1", "http://8.8.8.8/v1"])
def test_a_model_endpoint_off_this_network_is_refused(url):
    with pytest.raises(ModelError, match="no paid calls"):
        agent_mod.require_local(url)


def test_the_eval_harness_is_not_imported_by_other_commands():
    code = (
        "import sys\n"
        "from archi.bundles.cli import build_parser\n"
        "build_parser(load_ops=False).parse_args(['bundles', 'list'])\n"
        "print('archi.evaluation' in sys.modules, 'mcp' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "False False"
