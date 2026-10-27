"""req.r3-context-quality.context-eval, the raw-document baseline: the agent arms' loop, caps,
spend ledger, gold resolution, scorer and judge, all on fakes (no model call, no network).

The sandbox itself is tested in test_raw_sandbox.py and the corpus in test_raw_corpus.py.
"""
import asyncio
import json
import math
from pathlib import Path

import pytest
import yaml

from archi.bundles.cli import main
from archi.evaluation import agent as agent_mod
from archi.evaluation import raw_arm
from archi.evaluation.context import parse_context
from archi.evaluation.raw_arm import (
    AGENT_BASH,
    AGENT_GRAPH,
    ANSWERED,
    CAPPED,
    ERROR,
    JUDGE_OFF,
    NO_ANSWER_KEYS,
    STOPPED,
    AgentPlan,
    Caps,
    GoldError,
    GoldRecord,
    Judge,
    Ledger,
    Metered,
    Price,
    SpendError,
    ToolEvent,
    build_prompt,
    resolve_gold,
    run_agent_arms,
    run_try,
    score_evidence,
)
from archi.evaluation.raw_corpus import verify_corpus
from evaluation.raw_support import FakeMcp, ScriptedModel, failing, reply, search_rows, write_corpus

PRICE = Price(1.0, 10.0)  # $1 / $10 per million tokens


def metered(tmp_path, script, *, cap=10.0, price=PRICE, max_output=1000):
    ledger = Ledger(tmp_path / "ledger.jsonl", cap)
    return Metered(ScriptedModel(script), price, ledger, max_output)


def one_try(model, tools, *, caps=Caps(), clock=None, arm=AGENT_GRAPH):
    specs = asyncio.run(tools.list_tools())
    kwargs = {"clock": clock} if clock else {}
    return asyncio.run(run_try(model, tools, specs, arm=arm, system="sys", question_id="q1",
                               question="Why do transfers fail?", attempt=1, caps=caps, **kwargs))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


# --- the loop, the caps and the ledger -------------------------------------------------------


def test_the_loop_runs_tools_until_the_model_answers_and_ledgers_every_call(tmp_path):
    tools = FakeMcp({"transfer failures": search_rows("jira:DEMO-1")})

    def script(messages, turn):
        if turn == 1:
            return reply("", ("search", {"query": "transfer failures"}))
        if turn == 2:
            assert messages[-1]["role"] == "tool" and "jira:DEMO-1" in messages[-1]["content"]
            return reply("", ("inspect", {"target": {"kind": "node", "node_id": "jira:DEMO-1"}}))
        return reply("Stuck transfers.\nSources: jira:DEMO-1", prompt=2000, completion=50)

    model = metered(tmp_path, script)
    result = one_try(model, tools)
    assert result.status == ANSWERED and result.turns == 3 and result.tool_calls == 2
    assert result.answer.startswith("Stuck transfers") and all(e.ok for e in result.events)
    ledger = rows(tmp_path / "ledger.jsonl")
    assert len(ledger) == 3
    assert {r["arm"] for r in ledger} == {AGENT_GRAPH} and {r["model"] for r in ledger} == {"fake-model"}
    assert {r["role"] for r in ledger} == {"agent"}
    assert ledger[0]["prompt_tokens"] == 1000 and ledger[0]["completion_tokens"] == 100
    assert ledger[0]["cost_usd"] == pytest.approx((1000 * 1 + 100 * 10) / 1e6)
    assert result.cost == pytest.approx(sum(r["cost_usd"] for r in ledger))
    assert ledger[-1]["spent_usd"] == pytest.approx(model.ledger.spent)


def test_the_turn_cap_ends_a_try(tmp_path):
    model = metered(tmp_path, lambda m, t: reply("", ("search", {"query": "x"})))
    result = one_try(model, FakeMcp(), caps=Caps(max_turns=3))
    assert result.status == CAPPED and result.reason == "3 model turns" and result.turns == 3
    assert model.model.calls == 3


def test_the_tool_call_cap_ends_a_try_mid_turn(tmp_path):
    model = metered(tmp_path, lambda m, t: reply("", *[("search", {"query": str(i)}) for i in range(5)]))
    result = one_try(model, FakeMcp(), caps=Caps(max_tool_calls=7))
    assert result.status == CAPPED and result.reason == "7 tool calls" and result.tool_calls == 7
    assert result.turns == 2


def test_the_time_cap_ends_a_try(tmp_path):
    now = [0.0]

    def clock():
        now[0] += 400.0
        return now[0]

    model = metered(tmp_path, lambda m, t: reply("", ("search", {"query": "x"})))
    result = one_try(model, FakeMcp(), caps=Caps(try_seconds=900), clock=clock)
    assert result.status == CAPPED and result.reason == "900 seconds"


def test_a_model_call_never_waits_longer_than_the_try_has_left(tmp_path):
    seen = []

    class Slow(ScriptedModel):
        timeout = 600.0

        def complete(self, messages, tools):
            seen.append(self.timeout)
            return super().complete(messages, tools)

    model = Metered(Slow(lambda m, t: reply("done")), PRICE, Ledger(tmp_path / "l.jsonl", 1.0), 100)
    one_try(model, FakeMcp(), caps=Caps(try_seconds=30))
    assert seen and seen[0] <= 30 and model.model.timeout == 600.0


def test_a_model_failure_is_an_error_not_a_crash(tmp_path):
    result = one_try(metered(tmp_path, failing), FakeMcp())
    assert result.status == ERROR and "HTTP 500" in result.reason


def test_a_tool_response_is_cut_to_the_output_limit_both_arms(tmp_path):
    tools = FakeMcp({"big": "x" * 5000})
    model = metered(tmp_path, lambda m, t: reply("", ("search", {"query": "big"})) if t == 1 else reply("done"))
    result = one_try(model, tools, caps=Caps(output_bytes=1000))
    assert len(result.events[0].text) < 1100 and result.events[0].text.endswith("[cut at 1000 bytes]")


@pytest.mark.parametrize("cap", [None, 0, -1, float("nan"), float("inf"), True])
def test_the_ledger_refuses_to_start_without_a_cap(tmp_path, cap):
    with pytest.raises(SpendError, match="without a spend cap"):
        Ledger(tmp_path / "ledger.jsonl", cap)


def test_a_price_is_dollars_per_million_tokens():
    assert Price.parse("1.25/10") == Price(1.25, 10.0)
    assert Price.parse("1.25/10/0.125").cost(1_000_000, 0, 1_000_000) == pytest.approx(0.125)
    for bad in ("", "1", "a/b", "1/2/3/4", "-1/2", "0/0"):
        with pytest.raises(SpendError):
            Price.parse(bad)


def test_the_cap_stops_before_a_call_that_could_cross_it(tmp_path):
    # Each call's bound is about 0.0005 + 1000 output tokens at $10/M = $0.01; at $0.025 the third
    # call of the run could cross the cap, so it is never made.
    script = lambda m, t: reply("", ("search", {"query": "x"}), prompt=1000, completion=1000)
    model = metered(tmp_path, script, cap=0.025)
    result = one_try(model, FakeMcp())
    assert result.status == STOPPED and "cap" in result.reason
    assert model.model.calls == 2 and len(rows(tmp_path / "ledger.jsonl")) == 2
    assert model.ledger.spent <= 0.025


# --- gold resolution -------------------------------------------------------------------------


def context_set(questions, bundle="demo"):
    return parse_context({"bundle": bundle, "questions": questions})


def corpus_and_keys(tmp_path):
    corpus = verify_corpus(write_corpus(tmp_path))
    return corpus, corpus.keys(), corpus.manifest_paths()


def test_gold_ids_resolve_through_inspect_to_corpus_files(tmp_path):
    _, keys, manifest = corpus_and_keys(tmp_path)
    cset = context_set([{"id": "q1", "question": "q?", "gold": [
        "jira:DEMO-1", "documentation_page:abc123", "slurm_partition:submit:gpu", "ops_incident:inc-1", "hn:42"]}])
    tools = FakeMcp()
    gold = asyncio.run(resolve_gold(tools, cset.scored, keys, manifest)).records["q1"]
    by_id = {g.node_id: g for g in gold}
    assert by_id["jira:DEMO-1"].path == "jira/DEMO-1.json" and by_id["jira:DEMO-1"].key == "DEMO-1"
    assert by_id["documentation_page:abc123"].source == "docsite"  # found by its url attribute
    assert by_id["slurm_partition:submit:gpu"].path == "slurm/partitions/gpu.json"
    assert by_id["ops_incident:inc-1"].status == "unavailable" and "ops-record" in by_id["ops_incident:inc-1"].reason
    assert by_id["hn:42"].status == "unavailable" and "hypernews" in by_id["hn:42"].reason
    assert all(call[0] == "inspect" and call[2] == {"okg/result_format": "json"} for call in tools.calls)


def test_a_gold_id_that_no_longer_resolves_refuses_the_run_by_name(tmp_path):
    _, keys, manifest = corpus_and_keys(tmp_path)
    cset = context_set([{"id": "q1", "question": "q?", "gold": ["jira:DEMO-1", "jira:GONE-9"]},
                        {"id": "q2", "question": "q?", "gold": ["mystery:1"]}])
    with pytest.raises(GoldError) as err:
        asyncio.run(resolve_gold(FakeMcp(), cset.scored, keys, manifest))
    message = str(err.value)
    assert "q1: jira:GONE-9: inspect reported an error" in message
    assert "q2: mystery:1: its source demo.unknown_source is neither in the corpus nor listed as left out" in message
    assert "never scored as a miss" in message


def test_a_gold_id_whose_record_is_not_in_the_corpus_refuses_the_run(tmp_path):
    _, keys, manifest = corpus_and_keys(tmp_path)
    nodes = dict(raw_arm_nodes(), **{"jira:DEMO-7": ("jira_issue", "demo.jira", {"key": "DEMO-7"})})
    cset = context_set([{"id": "q1", "question": "q?", "gold": ["jira:DEMO-7"]}])
    with pytest.raises(GoldError, match="no record of jira in the corpus has its key"):
        asyncio.run(resolve_gold(FakeMcp(nodes=nodes), cset.scored, keys, manifest))


def raw_arm_nodes():
    from evaluation.raw_support import NODES

    return NODES


def test_gold_sources_override_resolution(tmp_path):
    _, keys, manifest = corpus_and_keys(tmp_path)
    cset = context_set([{"id": "q1", "question": "q?", "gold": ["chunk:ff01", "jira:GONE-9"],
                         "gold_sources": {"chunk:ff01": {"path": "jira/DEMO-2.json"},
                                          "jira:GONE-9": {"source": "jira", "key": "DEMO-1"}}}])
    tools = FakeMcp(nodes={})
    gold = asyncio.run(resolve_gold(tools, cset.scored, keys, manifest)).records["q1"]
    assert [(g.status, g.path, g.key) for g in gold] == [
        ("override", "jira/DEMO-2.json", "DEMO-2"), ("override", "jira/DEMO-1.json", "DEMO-1")]
    assert tools.calls == []  # inspect is never asked


def test_a_gold_sources_entry_that_names_nothing_refuses(tmp_path):
    _, keys, manifest = corpus_and_keys(tmp_path)
    cset = context_set([{"id": "q1", "question": "q?", "gold": ["x:1"],
                         "gold_sources": {"x:1": {"path": "jira/NOPE.json"}}}])
    with pytest.raises(GoldError, match="gold_sources path jira/NOPE.json is not in the corpus"):
        asyncio.run(resolve_gold(FakeMcp(), cset.scored, keys, manifest))


@pytest.mark.parametrize("row, message", [
    ({"gold_sources": {"other:1": {"path": "a"}}}, "not one of its gold ids"),
    ({"gold_sources": {"jira:DEMO-1": {"source": "jira"}}}, "must be {source, key} or {path}"),
    ({"answer": ""}, "`answer` must be a non-empty string"),
])
def test_bad_agent_keys_in_a_question_are_refused(row, message):
    from archi.evaluation.context import ContextSetError

    with pytest.raises(ContextSetError, match=None) as err:
        context_set([{"id": "q1", "question": "q?", "gold": ["jira:DEMO-1"], **row}])
    assert message in str(err.value)


# --- scoring ---------------------------------------------------------------------------------

DEMO1 = GoldRecord("jira:DEMO-1", "resolved", "jira", "DEMO-1", "jira/DEMO-1.json")
DEMO2 = GoldRecord("jira:DEMO-2", "resolved", "jira", "DEMO-2", "jira/DEMO-2.json")
GONE = GoldRecord("ops_incident:inc-1", "unavailable", "ops-record", reason="left out")
MANIFEST = frozenset({"jira/DEMO-1.json", "jira/DEMO-2.json", "docsite/x.json"})


def ev(text, ok=True, arguments=None):
    return ToolEvent("bash", arguments or {}, text, ok)


def test_gold_only_in_a_tool_input_never_counts():
    events = [ev("no match here", arguments={"command": "cat /corpus/jira/DEMO-1.json  # DEMO-1 jira:DEMO-1"})]
    evidence = score_evidence(events, [DEMO1], MANIFEST)
    assert evidence.found == [] and evidence.recall == 0.0 and evidence.first_rank is None


def test_gold_only_in_the_final_answer_never_counts(tmp_path):
    tools = FakeMcp({"q": search_rows("jira:DEMO-2")})
    model = metered(tmp_path, lambda m, t: reply("", ("search", {"query": "q"})) if t == 1
                    else reply("It is DEMO-1.\nSources: jira:DEMO-1 /corpus/jira/DEMO-1.json"))
    result = one_try(model, tools)
    evidence = score_evidence(result.events, [DEMO1, DEMO2], MANIFEST)
    assert evidence.found == ["jira:DEMO-2"] and evidence.missed == ["jira:DEMO-1"]


def test_gold_in_a_failed_tool_response_never_counts():
    evidence = score_evidence([ev("/corpus/jira/DEMO-1.json", ok=False)], [DEMO1], MANIFEST)
    assert evidence.found == []


@pytest.mark.parametrize("text", [
    "/corpus/jira/DEMO-1.json:3: transfer failures",     # rg output: the path
    "./jira/DEMO-1.json",                                # find from /corpus
    '{"key": "DEMO-1", "summary": "..."}',               # cat: the natural key
    "[1] jira:DEMO-1 (jira_issue) · source demo.jira",   # an MCP row: the node id
])
def test_a_path_a_natural_key_or_a_node_id_in_a_successful_response_counts(text):
    assert score_evidence([ev(text)], [DEMO1], MANIFEST).found == ["jira:DEMO-1"]


def test_a_longer_key_that_contains_the_gold_key_does_not_count():
    assert score_evidence([ev("see DEMO-10 and XDEMO-1")], [DEMO1], MANIFEST).found == []


def test_a_short_natural_key_counts_only_by_its_path_or_node_id():
    short = GoldRecord("slurm_partition:submit:gpu", "resolved", "slurm", "gpu", "slurm/partitions/gpu.json")
    manifest = MANIFEST | {"slurm/partitions/gpu.json"}
    assert score_evidence([ev("the gpu partition is down")], [short], manifest).found == []
    assert score_evidence([ev("/corpus/slurm/partitions/gpu.json")], [short], manifest).found == [short.node_id]


def test_mrr_ranks_the_first_gold_record_among_the_records_read_in_order():
    events = [ev("/corpus/docsite/x.json\n/corpus/jira/DEMO-2.json"), ev("nothing"), ev("/corpus/jira/DEMO-1.json")]
    evidence = score_evidence(events, [DEMO1, GONE], MANIFEST)
    assert evidence.first_rank == 3 and evidence.reciprocal_rank == pytest.approx(1 / 3)
    assert evidence.records_read == 3


def test_an_unavailable_gold_record_leaves_the_denominator_and_is_listed():
    evidence = score_evidence([ev("/corpus/jira/DEMO-1.json")], [DEMO1, DEMO2, GONE], MANIFEST)
    assert evidence.recall == 0.5 and evidence.unavailable == ["ops_incident:inc-1"]
    only_gone = score_evidence([ev("x")], [GONE], MANIFEST)
    assert only_gone.recall is None  # nothing to score: not a 0


# --- the prompt ------------------------------------------------------------------------------


def test_the_prompt_differs_between_arms_only_in_the_knowledge_paragraph():
    caps = Caps()
    bash = build_prompt(AGENT_BASH, caps, folders=["jira"], tools=["jq", "python3"]).split("\n\n")
    graph = build_prompt(AGENT_GRAPH, caps).split("\n\n")
    assert len(bash) == len(graph) == 4
    assert [p for i, p in enumerate(bash) if i != 1] == [p for i, p in enumerate(graph) if i != 1]
    assert bash[1] != graph[1] and "/corpus" in bash[1] and "jq, python3 are installed" in bash[1]
    assert "30 turns and 200 tool calls" in bash[2]


# --- a whole run: arms, judge, cap stop ------------------------------------------------------


QUESTIONS = [
    {"id": "q1", "question": "Why do transfers fail?", "gold": ["jira:DEMO-1"], "answer": "Stuck transfers."},
    {"id": "q2", "question": "What is upgraded?", "gold": ["jira:DEMO-2", "ops_incident:inc-1"]},
    {"id": "q3", "question": "Only an ops record", "gold": ["ops_incident:inc-1"]},
]


def agent_script(messages, turn):
    """Search for the question, then answer."""
    if messages[-1]["role"] == "user":
        return reply("", ("search", {"query": messages[-1]["content"]}))
    return reply("Stuck transfers.\nSources: jira:DEMO-1")


def judge_script(scores):
    it = iter(scores)
    return lambda messages, turn: reply(json.dumps({"score": next(it), "reason": "ok"}), prompt=500, completion=20)


def run_plan(tmp_path, *, cap=10.0, judge_scores=None, passes=1, questions=QUESTIONS, tries=1):
    corpus, keys, manifest = corpus_and_keys(tmp_path)
    cset = context_set(questions)
    tools = FakeMcp({"Why do transfers fail?": search_rows("jira:DEMO-1"),
                     "What is upgraded?": search_rows("jira:DEMO-9", "jira:DEMO-2")})
    model = metered(tmp_path, agent_script, cap=cap)
    judge = None
    if judge_scores is not None:
        judge = Judge(Metered(ScriptedModel(judge_script(judge_scores), "fake-judge"), Price(2.0, 8.0),
                              model.ledger, 1000), passes=passes)
    plan = AgentPlan((AGENT_GRAPH,), model, Caps(), tries, corpus, judge=judge,
                     judge_requested=judge_scores is not None)
    gold = asyncio.run(resolve_gold(tools, cset.scored, keys, manifest))
    report = asyncio.run(run_agent_arms(cset.scored, plan, tools, gold, manifest=manifest))
    return report, report.to_dict(), model


def test_a_run_scores_each_question_and_skips_one_with_only_unavailable_gold(tmp_path):
    report, data, _ = run_plan(tmp_path)
    s = data["arms"][AGENT_GRAPH]
    assert [q["id"] for q in data["questions"]] == ["q1", "q2"]  # q3 has no available gold
    assert s["tries"] == 2 and s["tries_scored"] == 2
    assert s["evidence_recall"] == 1.0  # q1 finds DEMO-1; q2 finds DEMO-2, its ops record is unavailable
    assert s["mrr"] == pytest.approx((1 + 0.5) / 2)  # q2 reads DEMO-9 first
    assert s["answer_score"] == JUDGE_OFF  # q1 has an answer key, but no judge ran
    assert data["gold"]["q2"]["ops_incident:inc-1"]["status"] == "unavailable"
    assert data["spend"]["calls"] == 4 and data["spend"]["stopped_at_cap"] is False
    assert data["corpus"]["digest"].startswith("sha256:") and "ops-record" in data["ops_record"]


def test_with_no_answer_keys_the_judge_column_says_so_never_0(tmp_path):
    questions = [{k: v for k, v in q.items() if k != "answer"} for q in QUESTIONS]
    report, data, _ = run_plan(tmp_path, questions=questions, judge_scores=[])
    assert data["arms"][AGENT_GRAPH]["answer_score"] == NO_ANSWER_KEYS
    from archi.evaluation.raw_arm import render_agent_markdown

    md = "\n".join(render_agent_markdown(data, {"k": 3, "arms": {}}))
    assert "| no answer keys |" in md and "judge: no answer keys" in md


def test_two_judge_passes_are_averaged_and_ledgered_as_judge_calls(tmp_path):
    report, data, model = run_plan(tmp_path, judge_scores=[1, 0.5], passes=2)
    assert data["arms"][AGENT_GRAPH]["answer_score"] == 0.75
    judged = [r for r in rows(tmp_path / "ledger.jsonl") if r["role"] == "judge"]
    assert len(judged) == 2 and {r["model"] for r in judged} == {"fake-judge"} and judged[0]["arm"] == AGENT_GRAPH
    assert data["judge"]["passes"] == 2 and data["judge"]["cost_usd"] > 0


def test_a_run_stops_cleanly_at_the_cap_and_reports_what_completed(tmp_path):
    # Each call costs $0.002 and is bounded at about $0.0104 (1000 output tokens at $10/M): at a
    # $0.013 cap the first try's two calls go through and the second try's first could cross it.
    report, data, model = run_plan(tmp_path, cap=0.013, tries=2)
    spend = data["spend"]
    assert spend["stopped_at_cap"] is True and "cap" in spend["stop_reason"]
    assert spend["spent_usd"] <= 0.013 and len(rows(tmp_path / "ledger.jsonl")) == spend["calls"] == 2
    statuses = [t["status"] for q in data["questions"] for ts in q["arms"].values() for t in ts]
    assert statuses == [ANSWERED, STOPPED]
    s = data["arms"][AGENT_GRAPH]
    assert s["tries_scored"] == 1 and s["stopped_at_cap"] == 1
    assert data["tries_run"] == 2 and data["tries_planned"] == 4


# --- the command -----------------------------------------------------------------------------


class Endpoint(FakeMcp):
    opened = 0

    def __init__(self, url, token):
        super().__init__({"Why do transfers fail?": search_rows("jira:DEMO-1"),
                          "What is upgraded?": search_rows("jira:DEMO-2"),
                          "Only an ops record": search_rows("jira:DEMO-2")})
        Endpoint.opened += 1


class FakeChat:
    made = []

    def __init__(self, base_url, model, **kwargs):
        self.name, self.kwargs, self.calls = model, kwargs, 0
        FakeChat.made.append(self)

    def complete(self, messages, tools):
        self.calls += 1
        if self.name.startswith("judge"):
            return reply('{"score": 1, "reason": "same"}', prompt=100, completion=10)
        return agent_script(messages, self.calls)


@pytest.fixture
def cli(monkeypatch, tmp_path):
    Endpoint.opened = 0
    FakeChat.made = []
    monkeypatch.setattr(agent_mod, "McpToolBox", Endpoint)
    monkeypatch.setattr(agent_mod, "OpenAIChat", FakeChat)
    monkeypatch.setenv("OKG_CHAT_MCP_TOKEN", "t0ken")
    monkeypatch.delenv("ARCHI_CONTEXT_EVAL_QUESTIONS", raising=False)
    questions = tmp_path / "questions.yaml"
    questions.write_text(yaml.safe_dump({"bundle": "demo", "k": 3, "questions": QUESTIONS}))
    return questions, write_corpus(tmp_path)


def agent_args(questions, corpus, out, *extra):
    return ["eval", "--context", "--questions", str(questions), "--mcp-url", "http://127.0.0.1:9/mcp",
            "--out", str(out), "--agents", "agent-graph", "--corpus", str(corpus), "--model", "m",
            "--model-price", "1/10", *extra]


def test_the_command_runs_the_agent_arms_beside_the_others_and_keeps_a_ledger(cli, tmp_path, capsys):
    questions, corpus = cli
    code = main(agent_args(questions, corpus, tmp_path / "out"))
    assert code == 0, capsys.readouterr().err
    data = json.loads((tmp_path / "out" / "report.json").read_text())
    assert set(data["arms"]) == {"graph", "baseline"}  # the no-model arms, untouched
    assert set(data["agent_arms"]["arms"]) == {AGENT_GRAPH}
    assert data["agent_arms"]["corpus"]["digest"] == verify_corpus(corpus).digest
    assert data["agent_arms"]["arms"][AGENT_GRAPH]["answer_score"] == JUDGE_OFF
    ledger = rows(tmp_path / "out" / "ledger.jsonl")
    assert ledger and {r["arm"] for r in ledger} == {AGENT_GRAPH}
    md = (tmp_path / "out" / "report.md").read_text()
    assert "## All arms side by side" in md and "| agent-graph |" in md and "| graph |" in md
    assert [c.name for c in FakeChat.made] == ["m"]  # no judge was built


def test_the_judge_is_off_unless_asked(cli, tmp_path):
    questions, corpus = cli
    from archi.bundles.cli import build_parser

    args = build_parser().parse_args(agent_args(questions, corpus, tmp_path / "out"))
    assert args.judge is False and args.judge_passes == 1


def test_the_judge_runs_when_asked_and_keys_exist(cli, tmp_path, monkeypatch):
    questions, corpus = cli
    key = tmp_path / "judge.key"
    key.write_text("sk-judge-test\n")
    monkeypatch.setenv("ARCHI_EVAL_JUDGE_KEY_FILE", str(key))
    code = main(agent_args(questions, corpus, tmp_path / "out", "--judge", "--judge-model", "judge-m",
                           "--judge-price", "2/8", "--judge-url", "http://127.0.0.1:8/v1", "--judge-passes", "2"))
    assert code == 0
    data = json.loads((tmp_path / "out" / "report.json").read_text())
    assert data["agent_arms"]["arms"][AGENT_GRAPH]["answer_score"] == 1.0
    judge = next(c for c in FakeChat.made if c.name == "judge-m")
    assert judge.kwargs["reasoning_effort"] == "xhigh" and judge.kwargs["api_key"] == "sk-judge-test"
    assert "sk-judge-test" not in (tmp_path / "out" / "report.json").read_text()
    assert "sk-judge-test" not in (tmp_path / "out" / "ledger.jsonl").read_text()


@pytest.mark.parametrize("extra, message", [
    (["--cap", "0"], "refuse to start without a spend cap"),
    (["--cap", "-5"], "refuse to start without a spend cap"),
])
def test_the_command_refuses_without_a_cap_and_spends_nothing(cli, tmp_path, capsys, extra, message):
    questions, corpus = cli
    assert main(agent_args(questions, corpus, tmp_path / "out", *extra)) == 2
    assert message in capsys.readouterr().err
    assert FakeChat.made == [] and Endpoint.opened == 0 and not (tmp_path / "out").exists()


def test_the_command_refuses_without_a_price(cli, tmp_path, capsys):
    questions, corpus = cli
    args = [a for a in agent_args(questions, corpus, tmp_path / "out") if a not in ("--model-price", "1/10")]
    assert main(args) == 2 and "--model-price" in capsys.readouterr().err


def test_a_gold_id_that_does_not_resolve_refuses_the_command_before_any_model_call(cli, tmp_path, capsys):
    questions, corpus = cli
    data = yaml.safe_load(questions.read_text())
    data["questions"][0]["gold"].append("jira:GONE-9")
    questions.write_text(yaml.safe_dump(data))
    assert main(agent_args(questions, corpus, tmp_path / "out")) == 2
    err = capsys.readouterr().err
    assert "q1: jira:GONE-9" in err and "never scored as a miss" in err
    assert all(c.calls == 0 for c in FakeChat.made) and not (tmp_path / "out" / "report.json").exists()


def test_a_corpus_that_changed_is_refused(cli, tmp_path, capsys):
    questions, corpus = cli
    (corpus / "records" / "jira" / "DEMO-1.json").write_text("tampered\n")
    assert main(agent_args(questions, corpus, tmp_path / "out")) == 2
    assert "changed since the corpus was built" in capsys.readouterr().err


def test_a_paid_endpoint_needs_the_opt_in(cli, tmp_path, capsys):
    questions, corpus = cli
    assert main(agent_args(questions, corpus, tmp_path / "out", "--model-url", "https://api.example.com/v1")) == 2
    assert "--allow-remote-model" in capsys.readouterr().err


def test_a_remote_model_needs_its_key_file(cli, tmp_path, capsys, monkeypatch):
    questions, corpus = cli
    monkeypatch.delenv("ARCHI_EVAL_MODEL_KEY_FILE", raising=False)
    assert main(agent_args(questions, corpus, tmp_path / "out", "--model-url", "https://api.example.com/v1",
                           "--allow-remote-model")) == 2
    assert "ARCHI_EVAL_MODEL_KEY_FILE" in capsys.readouterr().err


def test_the_cap_stop_exits_3_with_the_report_written(cli, tmp_path, capsys):
    questions, corpus = cli
    code = main(agent_args(questions, corpus, tmp_path / "out", "--cap", "0.013", "--max-output-tokens", "1000"))
    assert code == 3
    assert "stopped at the spend cap" in capsys.readouterr().err
    data = json.loads((tmp_path / "out" / "report.json").read_text())
    assert data["agent_arms"]["spend"]["stopped_at_cap"] is True
    assert data["status"] == "pass"  # the graph arm's own status is unchanged


def test_on_an_instance_the_ledger_lands_in_the_run_directory_and_the_gate_still_reads(cli, tmp_path, monkeypatch):
    from archi.ops.context_gate import GateSpec, context_eval_reading, run_dirs
    from archi.stack.instance import Instance
    from evaluation.test_context import instance_at

    questions, corpus = cli
    inst = instance_at(tmp_path, ["demo"])
    monkeypatch.setattr(Instance, "load", classmethod(lambda cls, d: inst))
    code = main(["eval", "--context", "--questions", str(questions), "--agents", "agent-graph", "--corpus",
                 str(corpus), "--model", "m", "--model-price", "1/10"])
    assert code == 0
    base = tmp_path / "data" / "eval" / "context" / "demo"
    runs = run_dirs(base)
    assert len(runs) == 1 and (runs[0] / "ledger.jsonl").is_file() and (runs[0] / "report.json").is_file()
    assert [p.name for p in base.iterdir() if p.name.startswith(".")] == []
    reading = context_eval_reading(GateSpec.from_mapping({"min_context_eval_score": 0.1}), inst.dir)
    assert reading.problem is None
