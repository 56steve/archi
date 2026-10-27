# archi eval

`archi eval` checks that an instance answers. It asks the questions of a
bundle's **smoke set** through a small agent that reads the instance only over
its [MCP endpoint](../connecting-an-agent.md), then checks each answer with
fixed rules. It needs a model, and the model is a local one, so a run costs
nothing.

A bundle that installs green can still answer nothing. The smoke set turns
"it works" into a check you can run.

!!! note "This harness is a stop-gap"
    The eval framework tracked in
    [#619](https://github.com/archi-physics/archi/issues/619) replaces it. The
    question files and the report format are kept so they can move across.
    The context eval (retrieval scored against a raw-document baseline) is
    part of that framework and is [still coming](#context-evals-still-coming).

## Run it

```bash
archi eval --bundle cern-team --model qwen3:8b
```

Run it from the instance directory, or pass `--instance DIR`. The instance
must be up (`archi up`): the agent connects to its MCP endpoint with the bearer
token in the instance `.env`. A model server must answer too. The default is
a local Ollama at `http://127.0.0.1:11434/v1`; `--model-url` names another.
Pick a model that can call tools; the agent has no other way to read the
graph.

| Option | Meaning |
|---|---|
| `--bundle X` | The bundle whose set to run. Repeat it for several. Default: the instance's bundles that ship a set. |
| `--instance DIR` | The instance directory (default: the current directory). |
| `--model M` | The model name at the endpoint. Default: the bundle's `chat.default_model`, if it sets one. |
| `--model-url URL` | An OpenAI-compatible base URL. Must be on this host or a private network. |
| `--out DIR` | Where to write the report. Default: `eval/<bundle>/` under the instance's data root, outside the instance repository. |
| `--mcp-url URL` | An MCP endpoint other than the instance's own. The instance is then not read at all: the bearer token comes from `OKG_CHAT_MCP_TOKEN`, `--bundle` is required, and there is no default chat prompt. An instance's token is never sent to another endpoint. |
| `--chat-prompt FILE` | The chat system prompt to give the agent. Default: the instance's `deployments/<name>/skills/chat-system-prompt.md`, the one `archi up` gives the chat. |
| `--max-steps N` | Tool-call rounds allowed per question (default 8). |
| `--no-think` | Ask a reasoning model to skip its thinking pass. |
| `--allow-remote-model` | Allow a model endpoint off this network. This can cost money; it is off by default. |

The command prints one line per question, writes `report.md` and
`report.json`, and exits:

| Exit | Meaning |
|---|---|
| 0 | The set passed at least the bundle's threshold. |
| 1 | It passed fewer than the threshold. |
| 2 | The run could not execute: the MCP endpoint offered no tools or was unreachable, the model failed on every question, a file was unusable, or the run stopped on an unexpected error. The report is still written; questions answered before the stop keep their result. |

## What the agent is given

The system message holds three things, in this order: the instructions the
MCP server serves in its `initialize` reply (how to search, filter and read),
the bundle's chat system prompt, and a short answer-format note. The first two
are what an operator's chat agent sees. The note comes last because it
overrides the chat prompt's citation style: the checks read node ids. Its
example ids are placeholders that no shipped set expects, so a model that
copies them gains nothing. The report records a hash of each part.

## What a question checks

Each bundle ships `bundles/<bundle>/eval/smoke.yaml`: at least ten questions
and a `pass_threshold`, currently **7 of 10** for both sets. The number is a
starting value; it will be raised once more than one model has results.

```yaml
- id: fts3-version
  question: Which FTS version does the FTS3 documentation describe?
  must_cite: {sources: [docsite]}   # ids, any_id, subtypes or sources
  must_contain: ["3.12"]            # also must_contain_any
```

The agent answers with the node ids it used. A question passes when:

- the answer names a node that a tool returned in this run (an invented id
  does not count), and that node meets every `must_cite` key given: its id is
  one required, or its subtype or source is one listed; and
- the answer holds every `must_contain` string, ignoring case, hyphens between words and extra spaces ("low-level" matches "low level").

There is no LLM judge. A wrong answer that cites the right source and uses the
right words passes; the set is a smoke check, not a grade. Results depend on
the model, and a small model may miss questions a larger one gets.

A question ends as **pass**, **fail** (the answer did not meet the rules, or
the model ran out of steps) or **execution_failed** (the model endpoint
errored). If the MCP endpoint has zero tools, the whole run is
`execution_failed`: an agent with nothing to read with cannot be scored.

## The report

`report.json` records the model, the MCP tools it saw, a hash of the prompt and
tool schemas, the threshold, and per question the status, the failure reasons,
the cited node ids with their subtype and source, each tool call, tokens in
and out, wall time and the answer text. `report.md` is the same for reading.

The [`instance-health`](../library/packs/instance-health.md) automation reads
every `report.json` under `<data root>/eval/` and raises an issue per bundle
when the latest run is `execution_failed`, below the threshold, or more than
`min_drop` below the earlier runs of the same model and question set. Each run overwrites the
report in the default `--out` directory, so to give it a trend, keep runs
apart: `archi eval --bundle cern-team --out <data root>/eval/cern-team/$(date +%F)`.

## The shipped sets

| Bundle | Corpus the questions rely on |
|---|---|
| `cern-team` | The bare defaults: the FTS3 and Rucio documentation pages and Indico event 1716863. Run it on an instance made with `archi init <name> --bundle cern-team` and no changes. |
| `cms-physics` | An invented fixture corpus: two analysis notes, three TWiki topics, two CDS records and the CMSSW 15_0 release map. `python/tests/evaluation/physics_lab.py` installs it into a scratch database. On a real CMS instance, this set checks only that the corpus is there. |

`cms-comp-ops` and `cluster-manager` have no set yet.

## In CI

The nightly workflow (`nightly-e2e`) runs `archi eval --bundle cern-team` after
the cited-answer step, with a small model on the CPU runner, and reports the
score in the job summary. A CI gate on bundle changes across all four bundles
is not built; it waits for the #619 framework, because a gate needs either a
slow local model or a paid API model.

## Context evals (still coming)

`archi eval --context` is not in this branch. It will score retrieval alone and
compare it with a baseline that reads raw documents. That score feeds the
[context gate](../context-layer.md#the-context-gate): an automation can
require a minimum score before it runs live. Until it exists, a gate that asks
for a context-eval score fails, so the automation stays in shadow.
