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
    The [context eval](#context-evals-archi-eval-context) (retrieval scored
    against a lexical baseline) needs no model and is described below.

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

When a question checks a cited node's subtype or source and no tool result
said it (a model that asks `filter` for only some fields gets rows without
them), the runner reads that node once with `inspect` after the answer, with no
model involved, and lists it under `lookups` in the report with whether the read
worked. Only a node a tool returned as a result is read, never an id the model
saw only inside another record's text, and only while the question's check is
not yet met. Only what that read says about the cited node counts.

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
It does not read [context evals](#context-evals-archi-eval-context): their
reports under `<data root>/eval/context/` carry another schema and are left
out.

## The shipped sets

| Bundle | Corpus the questions rely on |
|---|---|
| `cern-team` | The bare defaults: the FTS3 and Rucio documentation pages and Indico event 1716863. Run it on an instance made with `archi init <name> --bundle cern-team` and no changes. |
| `cms-physics` | An invented fixture corpus: two analysis notes, three TWiki topics, two CDS records and the CMSSW 15_0 release map. `python/tests/evaluation/bundle_lab.py` installs it into a scratch database. On a real CMS instance, this set checks only that the corpus is there. |
| `cluster-manager` | An invented cluster: four documentation pages (draining a node, scratch space, GPU jobs, maintenance windows), three Redmine tickets, and the ops records of two incidents. Nothing in it names a particular lab or experiment. `python/tests/evaluation/bundle_lab.py --bundle cluster-manager` installs it into a scratch database. |

`cms-comp-ops` has no set.

## In CI

The nightly workflow (`nightly-e2e`) runs `archi eval --bundle cern-team` after
the cited-answer step, with a small model on the CPU runner, and reports the
score in the job summary. A CI gate on bundle changes across all four bundles
is not built; it waits for the #619 framework, because a gate needs either a
slow local model or a paid API model.

## Context evals: `archi eval --context`

The context eval scores retrieval alone: did the search bring back the
evidence that answers the question? It asks no model. For each question it
calls the instance's MCP `search` tool directly and checks whether the
question's **gold** node ids, the evidence chosen by hand as the answer, come
back in the top *k* rows.

```bash
archi eval --context --questions /path/to/operator-questions.yaml
```

| Option | Meaning |
|---|---|
| `--context` | Run the context eval instead of the smoke set. |
| `--questions FILE` | The question file. Default: `$ARCHI_CONTEXT_EVAL_QUESTIONS`, else the bundle's `eval/context.yaml`. No bundle ships one, so name a file. A path that does not exist exits 2 and names the path. |
| `--k N` | The recall cut-off. Default: the file's `k`, else 10. |

`--instance`, `--mcp-url` and `--bundle` work as for the smoke set. The model
options are not used.

### The question file

The format is the smoke format with `gold` added, so a smoke file whose
questions carry `gold` ids can be used as it is (`pass_threshold`, `must_cite`
and `must_contain` are read and ignored):

```yaml
bundle: cern-team
k: 10                 # optional (default 10)
min_recall: 0.6       # optional: exit 1 when the graph arm's recall@k is below it
questions:
  - id: fts3-what-is
    question: What is FTS3?
    gold: [documentation_page:2275b50c]   # the node ids that answer it
```

A question without `gold` is listed in the report and not scored. The bundle
name is used in paths, so it may hold only letters, digits, `.`, `_` and `-`.

**Where the question set lives.** An instance's operator questions are
private. They are never committed to this repository, which is public. Keep
the file with the instance (or anywhere outside this repository) and name it
when you run:

```bash
archi eval --context --questions /path/to/operator-questions.yaml
# or
export ARCHI_CONTEXT_EVAL_QUESTIONS=/path/to/operator-questions.yaml
archi eval --context
```

`--questions` wins over the variable. The only question file in this
repository is the test fixture, `python/tests/evaluation/fixtures/context.yaml`,
whose ids are invented. On an instance, a set whose `bundle` the instance does
not list is refused (exit 2), so a fixture or another instance's set cannot
produce a run the context gate would read.

### The two arms

Each question runs twice, in two **arms** (two ways of searching):

| Arm | What it calls |
|---|---|
| `graph` | `search` with the server's defaults: the search an operator's agent makes. Passages roll up to their records. |
| `baseline` | `search` with `method: lexical`, `rank_by: self` and `expand_concepts: false`: the server's lexical index over chunks and attributes, each text unit ranked on its own words, with no roll-up to records and no concept expansion. |

The baseline is a **lexical baseline**, the closest the MCP surface comes to
keyword search over the raw documents. It is not a separate index of the raw
documents: it searches the same server's lexical index, which covers chunks
and node attributes. Chunk rows can fill its *k* slots where the graph arm
returns one row per record, so part of the difference between the arms is
that roll-up, not the graph's links. A question file can replace either arm,
or add more, under `arms:`, for example `baseline: {tool: search, arguments:
{method: lexical, where: {type: document_chunk}}}`. The harness always sets
`query` (the question) and `limit` (k) itself.

Each call asks okg for JSON (`_meta` `okg/result_format: json`, which okg
0.2.0 serves). A server that answers in text instead is read from its row
lines, `[n] <id> (<subtype>)` at the start of a line; an indented line is part
of an excerpt and never a row. A row counts as a hit for a gold id when the
row's id is that id, or when the row is a passage whose record (`parent` in
JSON, `in <id>` on a text row line) is that id. Ids in a row's title or
excerpt do not count. The generation each call read is taken from the reply.

### The report

Each run gets its own directory, so the history is kept:
`<data root>/eval/context/<bundle>/<UTC stamp>/report.json` and `report.md`
(the stamp is `YYYYMMDDTHHMMSSZ`, with `-2`, `-3`, ... added when two runs
finish in the same second). With `--mcp-url` the default is
`eval-reports/context/<bundle>/<UTC stamp>/`; `--out DIR` writes to that
directory instead.

The report holds, per arm, recall@k (the share of each question's gold ids
found in the top k, averaged over questions), MRR (the mean of 1 / rank of the
first gold hit) and the number of failed calls; the total of failed calls
(`errors`); the baseline delta (graph minus baseline recall@k), computed only
over the questions on which both arms executed; the generation the graph
arm's calls read; the question file's path, its sha256 and the number of
scored questions; and per question what each arm found and missed.

| Exit | Meaning |
|---|---|
| 0 | The run finished (and met `min_recall`, if the file sets one). |
| 1 | The graph arm's recall@k is below `min_recall`. |
| 2 | The run could not execute or has no score: the endpoint was unreachable, it has no `search` tool, any graph-arm call failed, a file was unusable, or the set's bundle is not the instance's. |

A failed baseline call counts as a baseline miss and is listed; a failed
graph call leaves the run without a score.

### The score and the context gate

The **score** is the graph arm's recall@k. The
[context gate](../context-layer.md#the-context-gate) of an automation with
`min_context_eval_score` reads the newest run of one bundle under
`<data root>/eval/context/<bundle>/`: the gate's `context_eval_bundle`, or the
instance's only bundle. Only that newest run counts. The `context_eval` check
fails, naming the reason, when that run:

- cannot be read, or is not an `archi-context-eval-report/1` report of that bundle;
- did not execute cleanly (status `execution_failed`, or any graph-arm call failed);
- lacks its question-file hash, or its scored-question count does not match its rows;
- finished more than `max_age_days` ago (a gate key; default 14 days);
- or scores below `min_context_eval_score`.

A run written elsewhere with `--out`, or against `--mcp-url`, is not read by
any gate.

The `instance-health` automation does **not** read context evals. It reads
only the smoke reports (`archi-eval-report/1`) and leaves context-eval reports
out.
