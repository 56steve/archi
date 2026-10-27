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
    against a lexical baseline) needs no model and is described below. Its
    [raw-document baseline](#the-raw-document-baseline-raw-corpus-and-agents)
    adds two agent arms that do use a model, under a spend cap.

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
It does not read [context evals](#context-evals-archi-eval-context): it skips
`<data root>/eval/context/` and hidden directories.

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
options are used only by the [agent arms](#the-raw-document-baseline-raw-corpus-and-agents).

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
that roll-up, not the graph's links. The `graph` arm is lexical search too
(okg's default method), with concept expansion and record roll-up on, so the
delta measures those two, not graph links. A question file can replace either
arm, or add more, under `arms:`, for example `baseline: {tool: search,
arguments: {method: lexical, where: {type: document_chunk}}}`. The harness
always sets `query` (the question) and `limit` (k) itself. A file that
replaces the `graph` arm makes the run informational only: on an instance it
is refused (exit 2) unless written elsewhere with `--out`, and the context
gate refuses any report whose graph arm is not the default.

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
finish in the same second). A run is written into a hidden
`.<stamp>.tmp-<pid>` directory beside it and renamed into place only when both
files are complete, so a crash never leaves a half-written report where a
reader looks; readers skip names that start with a dot. With `--mcp-url` the default is
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
| 3 | With `--agents`: the agent arms stopped at the spend cap. The report holds what completed. |

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
- ran a graph arm the question file overrode;
- lacks its question-file hash, or its scored-question count does not match its rows;
- finished more than `max_age_days` ago (a gate key: above 0, at most 3650; default 14 days), or
  more than five minutes in the future;
- used another k than `context_eval_k`, or scored fewer questions than
  `context_eval_min_questions` (both optional gate keys, unset by default);
- or scores below `min_context_eval_score`.

A run written elsewhere with `--out`, or against `--mcp-url`, is not read by
any gate.

The `instance-health` automation does **not** read context evals. It reads
only the smoke reports (`archi-eval-report/1`) and never looks under
`<data root>/eval/context/` or in hidden directories, so a broken context-eval
file cannot break its eval signal.

## The raw-document baseline: `--raw-corpus` and `--agents`

The two arms above search the same server. The raw-document baseline asks a
different question: does the graph help an agent find the evidence, compared
with the same agent reading the files the graph was built from? It adds two
**agent arms** to a context run. Both use one model, one prompt and one set of
caps; they differ only in where the knowledge is.

| Arm | What the agent has |
|---|---|
| `agent-bash` | One tool, `bash`, over the raw corpus. The command runs in a [bubblewrap](https://github.com/containers/bubblewrap) sandbox: no network, the corpus mounted read-only at `/corpus`, a fresh in-memory `HOME`, an empty environment, and `rg`, `jq` and `python3` on `PATH` when the host has them. Each command may run 120 s and print 64 KiB; one that goes over either is stopped. |
| `agent-graph` | The instance's MCP tools. |

The prompt is the same text for both, except the one paragraph that says where
the knowledge is. The report holds both prompts.

These arms call a model, which may be a paid one. Every call is written to a
spend ledger, and the run stops at a cap you set.

### Build the corpus

```bash
archi eval --raw-corpus --bundle cms-comp-ops
```

This reads the instance's `source_registry.yaml` and takes each enabled source
whose connector cache an [`archi.snapshot`](../reference/connector-caches.md)
group reads. Each cache is packed with the snapshot builder, which checks every
file with the source's own reader, removes email addresses and drops the fields
the reader does not read. The archives are then unpacked, one file per record,
to `<data root>/eval/raw-corpus/<bundle>/<lock sha>/`, where `<lock sha>` is
the start of the snapshot lock's SHA-256:

| File | What it holds |
|---|---|
| `records/<group>/...` | One file per record. Only this directory is mounted into the sandbox. |
| `MANIFEST.txt` | `<path>` and `<sha256>` of every record file. The **corpus digest** is the SHA-256 of this file. |
| `keys.json` | Per source, the names a node id may give (natural key, node id) mapped to a file, and each file's natural key. |
| `corpus.json`, `README.md` | The digest, the sources, and every source left out with the reason. |

A source with no cache for the snapshot to check (a live fetch such as
`indico_live`, `cds` or MONIT, a git checkout, a derived source) is left out
and named. A cache the snapshot refuses refuses the whole corpus, and nothing
is written. Derived files and okg state are never read.

**ops-record is left out of both arms.** Its records exist only in the graph,
written through okg admission; there is no cache. An export through the MCP
tools could not be shown to be complete, so the corpus does not hold it, and
its gold records count as *unavailable* in both agent arms: they leave the
denominator and are listed. The `agent-graph` arm can still read ops records
through its tools, but they earn it nothing.

### Run the agent arms

```bash
export ARCHI_EVAL_MODEL_KEY_FILE=/path/to/file-holding-the-key
archi eval --context --questions /path/to/operator-questions.yaml \
  --agents --model <model> --model-url https://api.openai.com/v1 --allow-remote-model \
  --reasoning-effort medium --model-price IN/OUT/CACHED --tries 3 --cap 10
```

| Option | Meaning |
|---|---|
| `--agents [ARMS]` | Run the agent arms too: both by default, or `agent-bash` or `agent-graph` alone. |
| `--tries N` | Tries per question and arm (default 1). |
| `--corpus DIR` | The corpus. Default: the newest under `<data root>/eval/raw-corpus/<bundle>/`. |
| `--model`, `--model-url` | The model, at any OpenAI-compatible endpoint. A remote one needs `--allow-remote-model`. |
| `--model-key-env VAR` | The variable that holds the **path** of a file with the API key (default `ARCHI_EVAL_MODEL_KEY_FILE`). The key is never logged, never written to the report or ledger, and never enters the sandbox. |
| `--model-price IN/OUT[/CACHED]` | Dollars per million input, output and cached-input tokens. Required: the ledger needs it. |
| `--reasoning-effort E` | Sent with each agent call. |
| `--max-turns`, `--max-tool-calls`, `--try-seconds` | Caps per try: 30 model turns, 200 tool calls, 900 s by default. |
| `--max-output-tokens N` | Output tokens per call (default 16000); also the spend guard's bound. |
| `--cap DOLLARS` | The spend cap for the run, judge included (default 10). |
| `--judge` | Grade answers with a judge model. **Off by default.** |
| `--judge-model`, `--judge-url`, `--judge-reasoning-effort`, `--judge-key-env`, `--judge-price` | The judge: any OpenAI-compatible endpoint (default `https://api.openai.com/v1`, reasoning `xhigh`, key file path in `ARCHI_EVAL_JUDGE_KEY_FILE`). |
| `--judge-passes 1\|2` | Judge each answer once or twice; the score is the mean. |

Before any model call the run refuses, exit 2, naming the reason, when: there
is no cap or no price; the corpus's files no longer match its manifest, or it
belongs to another bundle; `bwrap` is not installed and `agent-bash` was asked
for; or any gold id does not resolve (below). Nothing is installed for you.

### How gold ids map to files

Each gold id is read with the MCP `inspect` tool, which returns its subtype,
attributes and source. The source and the node id (or an attribute such as a
URL) are looked up in `keys.json`. A question can name a record directly
instead:

```yaml
questions:
  - id: transfer-errors
    question: Why do transfers to the site fail?
    gold: [jira:CMSTRANSF-123, chunk:9f2a1c]
    answer: The storage certificate expired.     # optional answer key, for --judge
    gold_sources:                                # optional; overrides inspect for these ids
      chunk:9f2a1c: {path: jira/CMSTRANSF-123.json}
```

A gold id whose source is left out of the corpus (ops-record, a live source)
is **unavailable**. A gold id that `inspect` cannot find, or whose record is
not in the corpus, makes the run **refuse to score** and names the id and the
reason. It is never counted as a miss.

To check that gold ids keep naming the same records from one published
generation to the next, sample 20 of them at two generations:

```bash
python -m archi.evaluation.gold_check --questions FILE \
  --generation-a <older generation> --generation-b <newer generation> [--corpus DIR]
```

It exits 0 when every sampled id is visible at both with the same subtype,
source and (with `--corpus`) record, and 1 otherwise. It calls only MCP.

### How the agent arms are scored

The same scorer reads both arms' transcripts:

- **Evidence recall**, per try: the share of the question's available gold
  records whose corpus path, natural key or node id appears in a *successful*
  tool response. What the agent typed (tool inputs) and its final answer never
  count. A bash command succeeds when it exits 0 within its limits; an MCP call
  when the server reports no error. A natural key shorter than four characters
  counts only through its path or node id.
- **MRR**: 1 / the rank of the first gold record among the distinct records the
  agent read, in the order it read them.
- **Answer score**: with `--judge`, the judge's mean score (0, 0.5 or 1) over
  the answers to questions that have an `answer`. Without answer keys the
  column says **no answer keys**; with keys but no `--judge`, **judge off**.
  Never 0 for a missing key.

The report keeps `graph` and `baseline` as they are and adds the agent arms
under `agent_arms`, with one table of all four side by side: evidence recall
(recall@k for the search arms), MRR, answer score, cost per try, tool calls
per try, and failures (errors, tries that hit a cap). It also records the
corpus digest, the model, the caps, both prompts, the resolved gold map and
the spend. The context gate still reads only the graph arm's recall@k.

### Spend

Each model call, agent or judge, adds a row to
`<data root>/eval/context/<bundle>/<stamp>/ledger.jsonl`: the arm, the
question and try, the model, input, cached, output and reasoning tokens, and
the cost. During the run the ledger is written in a hidden directory beside
the runs and moved into the run's directory with the reports; with `--out` it
is written there directly.

Before each call the run bounds its cost: the request's size in bytes / 3 for
input tokens, plus `--max-output-tokens`. When that bound would take the spend
past `--cap`, the call is not made. The run stops cleanly, the report says
what completed (a try cut short is listed, not scored), and the command exits
**3**.
