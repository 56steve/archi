# Packs and bundles

A **pack** is the smallest installable capability: one source family, a set
of live tools, or one automation. A **bundle** is a named selection of packs
plus a chat preset and an eval set. The design is ADR 0002 §3; this page is
the reference for the files.

The published JSON schemas are
[`pack.schema.json`](../python/archi/bundles/schema/pack.schema.json) and
[`bundle.schema.json`](../python/archi/bundles/schema/bundle.schema.json).
Both refuse unknown keys.

## Layout

```
bundles/
  packs/<pack>/pack.yaml
  <bundle>/bundle.yaml
```

Every path inside a manifest is relative to the directory holding it and may
not leave that directory (`..`, absolute paths and `~` are refused).

## pack.yaml

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Equals the directory name. Lowercase words joined by `-`. |
| `kind` | yes | `context` (sources), `tool` (live tools) or `automation`. |
| `version` | yes | `MAJOR.MINOR.PATCH`. |
| `description` | yes | One sentence. |
| `needs_credentials` | yes | `true` when the pack cannot publish without a credential. |
| `requires.modules` | | okg ontology modules the pack needs. |
| `requires.packs` | | Other packs it needs. |
| `sources` | context packs | Directory of templates, one `<source>.yaml` per source. Its single top-level key is `<source>`. A context pack with no sources must declare `schemas` (records reach it through okg admission, as in `ops-record`). |
| `schemas` | | Directory of LinkML schema slices. |
| `init_questions` | | File holding an `init_questions:` list, in okg's profile question format. |
| `invariants` | | File holding an `invariants:` list. The pack owns them. |
| `freshness` | context packs with sources | `{<pack>.<source>: {max_staleness: 24h}}`, one entry for every source. A positive integer and one unit of `s m h d w` (no ISO-8601). `archi doctor` compares each source's age to it. |
| `link_coverage` | | List of link-coverage targets, see [Context quality](#context-quality). |
| `playbooks` | | Directory of playbooks. |
| `skill_triggers` | | File with `version:` and an `intents:` mapping. |
| `search_subtypes` | | Subtypes added to the `content` search profile. |
| `search_profiles` | | Named okg search profiles, `{<name>: {description, subtypes}}`, added to the instance's `search.profiles`. Not `content`. |
| `live_tools` | | `archi.tools.*` entries for the MCP server. |
| `action_classes` | | File holding an `action_classes:` mapping keyed by id (automation packs). |
| `automation` | automation packs | The automation: trigger, context gate, playbook, action classes, proposal schema, budget, levels. See [Automation packs](#automation-packs). |
| `files` | | Files installed at the same path in the deployment root (okg's `files:` allow-list applies). |
| `prompt_fragment` | | Markdown appended to the chat system prompt. |
| `credentials` | | Environment-variable **names** the pack reads. |

Source templates may reference three kinds of answers: the base questions
every instance has (`deployment_name`, `postgres_dsn`, `archi_data_root`,
`chat_site_name`, `chat_model`; see
[`base/questions.yaml`](../python/archi/bundles/base/questions.yaml)), the
pack's own questions, by their local id (`${site_url}`), and the questions of
a pack listed in `requires.packs`, by their rendered id
(`${git_code_repo_name}` for `git-code`'s `repo_name`). The last kind is
checked when the packs are composed: a reference no selected pack asks stops
the render.

## bundle.yaml

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Equals the directory name. |
| `version` | yes | `MAJOR.MINOR.PATCH`. |
| `description` | yes | One sentence. |
| `packs.default` | yes | Packs installed unless removed with `--without`. |
| `packs.optional` | | Packs offered for `--with`. |
| `extends` | | Bundles whose default packs this one inherits. |
| `chat` | | `site_name`, `default_model`, `description`, `suggestions`. |
| `automations` | | `{<automation pack>: {max_level}}`: an upper bound on that automation's level. See [Levels](#levels). |
| `eval` | | Eval set (JSONL). |
| `okg` | yes | Supported AKMON range, PEP 440 (`">=0.2.0,<0.3"`). |

## Checking manifests

```bash
archi bundles validate                 # every pack and bundle archi ships
archi bundles validate path/to/pack    # one pack or bundle directory
```

Besides the schema, `validate` refuses:

- a path the manifest names that does not exist;
- a credential written as a value: a `credentials` entry that is not an
  environment-variable name, a credential-named key in a source template
  holding a literal instead of `${VAR}`, or a credential question with a
  default;
- a template reference to a question that neither the base nor the pack
  declares, unless it names a required pack's question (`${<pack>_<id>}`,
  checked when the packs are composed);
- a `freshness` key that is not one of the pack's own sources, a value that is
  not a duration, or a context pack source with no `freshness` entry;
- a `link_coverage` target whose edge (`from -[edge]-> to`, or the reverse for
  `direction: in`) no source of the pack declares in its `output_signature`;
- an automation pack that breaks the rules in [Automation packs](#automation-packs), including any level above L2;
- a bundle that names an unknown pack or bundle, extends itself in a cycle,
  or has no credential-free default context pack with sources (taking
  inherited defaults into account), because a bundle with nothing configured
  must publish.

It prints every problem it finds and exits 1 if there is any.

## Composing an instance

```bash
archi render --bundle cern-team --with indico,git-code --instance ~/myteam
```

`render` selects packs by ADR 0002 §3.3:

```
packs = closure(requires, ∪ default(extends*(A, B, ...))) ∪ with − without
```

in a fixed *resolution order*: each `--bundle` in the order given, preceded
by the bundles it extends (parents first); their default packs, each preceded
by the packs it requires; then `--with` packs; then `--without` removals.
`requires` is closed over the defaults only, so a `--with` pack whose
requirement is not selected stops the render with the `--with` that fixes it,
and so does a `--without` that removes a pack another selected pack needs.

It then merges the packs and writes one okg profile to
`<instance>/profiles/<name>/` (`<name>` defaults to the bundle names joined
by `-`). okg looks in `./profiles/`, so running okg from the instance
directory finds it with no `OKG_PROFILES_DIR`.

| Part | Rule | Conflict |
|---|---|---|
| modules | union | — |
| schemas | union by relative path | same path, different bytes: stops, printing both digests |
| sources | id `<pack>.<source>`; file `source-defaults/<pack>.<source>.yaml` | — |
| init questions | base questions, then each pack's as `<pack>_<id>` (flag `--<pack>-<id>`) | same id, different definition: stops |
| invariants | union; each stays with its pack | same name, different body: stops |
| freshness | union; each stays with its pack | — |
| link coverage | each target becomes an invariant `<pack>_link_<name>`, owned by its pack | same invariant name, different body: stops |
| search `content` subtypes | union | — |
| other search profiles | union by name | same name, different body: stops |
| playbooks, skill triggers | union by file name / intent id | different content: stops |
| action classes | union by id | different body: stops |
| files | union by path | different bytes: stops |
| chat prompt | the base prompt, then each `prompt_fragment` in resolution order | a pack shipping `chat-system-prompt.md` or `skill-triggers.yaml` as a playbook: stops |
| chat scalars | from the last `--bundle` given (a child before its parents); other values are printed as notes | — |
| chat suggestions | union | — |
| `okg` range | intersection | empty: stops |

A pack that is not selected contributes nothing: rendering a selection with
`--with P --without P` gives the same bytes as rendering it without `P`. The
profile directory is rebuilt from scratch on every render.

The base questions, deployment defaults and chat prompt every profile starts
from are in [`python/archi/bundles/base/`](../python/archi/bundles/base/).

## Packaging and discovery

The `archi-kb` wheel carries `bundles/` and `skills/` as real files under
`archi/_payload/` (symlinks in the repository are followed at build time).
`archi bundles list` and `archi bundles show NAME` find packs and bundles
through two entry-point groups, so another package can add its own:

```toml
[project.entry-points."archi.packs"]
mywg = "mywg_packs:packs_dir"      # returns a directory of <pack>/pack.yaml
[project.entry-points."archi.bundles"]
mywg = "mywg_packs:bundles_dir"    # returns a directory of <bundle>/bundle.yaml
```

The value is a callable returning the directory, or the directory itself;
the name labels the provider in `archi bundles list`. Two providers shipping
a pack or bundle of the same name, or an entry point that fails to load, are
listed as problems and do not hide the rest.

A bundle directory that holds an okg `profile.yaml` and no `bundle.yaml` is
listed as a legacy profile, and `archi render --bundle <name> --instance DIR`
copies it, symlinks followed, to `DIR/profiles/<name>/`. Every bundle archi
ships is now a `bundle.yaml` (cern-team since PACT `r3-packs-split`). Either
way the profile lands in `<instance>/profiles/`, and okg run from the instance
directory finds it, so no step sets `OKG_PROFILES_DIR`.

## archi.lock, archi add, archi diff

`archi render` also writes `<instance>/archi.lock`. Commit it with the
instance. It records the selection (bundles, `--with`, `--without`), the okg
range and the okg version it was rendered with, each bundle's and pack's
version, provider and content digest, a sha256 of every rendered profile
file, and what the renderer composed that okg does not read: freshness SLOs,
live tools, action classes, and the pack that owns each source.
`archi render --deployment DIR` also records the installed okg deployment.

```bash
archi add indico --deployment deployments/myteam --answer indico_base_url=https://indico.cern.ch
archi diff            # exit 0: no drift; 1: drift; 2: no usable lock
```

`archi add <pack>` re-renders the instance with one more pack (and whatever
it requires that the instance lacks), then adds to the installed deployment only what is new: the
pack's registry entries with this deployment's answers filled in, schema
slices, files, playbooks, invariants, skill triggers, modules and search
subtypes. A file the deployment already holds with different content stops
the add; archi does not overwrite what the instance changed. If the
deployment is in a git repository the change is committed. Then archi runs
the okg steps the quickstart used to list as manual ones:

```
okg catalog ownership claim --deployment DIR --json
okg catalog load --deployment DIR --apply --json
okg ingest --deployment NAME --include <the new source ids>
okg run --once --deployment NAME --apply
```

`--no-okg` stops after the edit and the commit. New questions are answered
with `--answer <rendered id>=<value>`; any not answered take their defaults.

`archi diff` reports three kinds of drift: rendered profile files edited,
added or missing since the render; packs and bundles whose installed version
or content no longer matches the lock; and, for the recorded deployment,
registry entries that differ from what the rendered profile produces with
the deployment's own install answers (an entry edited by hand, removed, or
added outside archi).

## Context quality

ADR 0002 section 6.1 asks that the context layer be measured, not assumed.
Packs carry three of the measures; the fourth, the context eval, is a separate
change.

**Freshness.** Every source of a context pack declares `max_staleness` in
`freshness:`. No source template sets a schedule (their triggers are `manual`
and `reconcile`), so each value is a judgment: how old the source may get
before an answer from it is likely wrong, written in a comment above the
`freshness:` block. `archi render` copies the values into `archi.lock`
(`composed.freshness`).

`archi doctor` has a `source-freshness` check. It runs `okg doctor --check
sources --deployment <name> --json` and reads each source's
`coverage.watermark.last_reconcile_at`, the time of its last completed
reconcile in okg's `source_watermarks`. A source is late when that age passes its
`max_staleness`, and also when okg lists no run for it. `archi doctor --json`
lists every source in the check's `data.sources`:

```json
{"source": "monit-live.monit_sam", "age": "40h", "max_staleness": "36h", "ok": false,
 "age_seconds": 144000, "max_staleness_seconds": 129600,
 "last_reconcile_at": "2026-10-23T20:00:00+00:00", "reason": "last run 40h ago, over the 36h limit"}
```

The check is skipped, and says so, for an instance with no `archi.lock`.

**Link coverage.** A pack that carries identifiers other sources refer to (sites,
datasets, runs, releases, tickets) declares how well they must link:

```yaml
link_coverage:
  - name: storage_endpoint_site   # invariant: cms_catalogs_link_storage_endpoint_site
    from: storage_endpoint        # nodes that carry the identifier
    edge: contains
    to: site
    direction: in                 # `in`: site -[contains]-> storage_endpoint; default `out`
    min_fraction: 0.9             # share of `from` nodes that need such an edge
    severity: warn                # default; `error` blocks the publish
```

Composition turns each target into an okg invariant in the `invariants.yaml`
format. It returns one row, with the counts, when the share of linked nodes
falls short, and no row when the subtype is empty (the pack's coverage
invariant reports that). Targets are `warn` so that thin data cannot block a
publish; set `error` where a miss should. Declare a target only for an edge the
pack already emits.

**Context gate.** `archi.ops.context_gate` evaluates a gate (sources, maximum
staleness, minimum context-eval score, required invariants) against the
current generation's facts and returns pass, or fail with each failing check
named (`freshness:<source>`, `context_eval`, `invariant:<name>`). Facts come
from `archi doctor --json` (`facts_from_doctor`), plus invariant results and the
context-eval score from the caller. A missing score or result is a failure.

## Automation packs

An automation pack (`kind: automation`) declares one automation and the typed
actions it may propose (ADR 0002 sections 7.2 and 7.3). The pack holds the
declaration only. The runtime that runs it is PACT `r3-ops-runtime`.

One pack declares one automation, in an `automation:` block of `pack.yaml`.
The action classes live in the pack's `action_classes` file, which the
composer already merges by id across packs; the block lists the ids this
automation may propose.

```yaml
name: ticket-triage
kind: automation
version: 0.1.0
description: Reads new tickets and drafts or proposes a response.
requires: {packs: [redmine]}          # packs whose live tools act as mediators
playbooks: playbooks/
action_classes: action_classes.yaml
needs_credentials: false
automation:
  trigger: {type: ticket_queue, system: redmine, queue: cms-comp-ops}
  context_gate:                       # the keys of archi.ops.context_gate.GateSpec
    sources: [redmine.tickets]
    max_staleness: 24h
    min_context_eval_score: 0.8
    invariants: [redmine_link_ticket_site]
  playbook: triage.md                 # under playbooks/
  action_classes: [draft_reply, set_ticket_status]
  proposal_schema:                    # JSON Schema; see below
    type: object
    required: [summary, evidence_ids, action_class, payload, confidence, rollback]
    properties:
      summary: {type: string}
      evidence_ids: {type: array, minItems: 1, items: {type: string}}
      action_class: {type: string}
      payload: {type: object}
      confidence: {type: number, minimum: 0, maximum: 1}
      rollback: {type: string}
  budget: {cost_per_run: 0.5, cost_per_day: 10, runs_per_hour: 20}   # USD
  default_level: L1      # where it starts when the instance sets no level
  max_level: L2          # the most this pack allows
```

```yaml
# action_classes.yaml
action_classes:
  set_ticket_status:
    payload_schema: {type: object, required: [ticket_key, status], properties: {...}}
    mediator: archi.tools.redmine:set_status   # a live tool, or a workflow step name
    reversible: true
    blast_radius: One ticket's status field.
    max_level: L2
```

Choices worth knowing:

- **Trigger.** `schedule` needs `cron` (five fields). `ticket_queue` needs
  `system` (`redmine`, `jira` or `servicenow`) and `queue`. `signal` needs
  `source` (a rendered source id) and `condition` (text). A cron that can fire
  more often than `budget.runs_per_hour` is refused. The check reads the
  minute and hour fields (numbers, ranges, lists, `*` and steps) and counts the
  busiest hour; the day, month and weekday fields are treated as unrestricted,
  so it is an upper bound. It does not handle `@daily` style macros or names
  (`MON`, `JAN`) in the minute or hour field; those are refused.
- **Context gate.** The same keys and the same parser as the runtime gate
  (`archi.ops.context_gate.GateSpec`). The gate must name at least one check,
  because a gate that checks nothing cannot make the automation depend on
  measured context (D12).
- **Mediator.** `archi.tools.x:fn` must appear in `live_tools` of this pack or
  of a pack in `requires.packs`. Required packs are found through the catalog,
  so they may come from any pack root: a third-party automation can require a
  shipped pack. Anything else must be a registered *workflow step*. Steps live
  in the registry `archi.ops.steps`, the one place the runtime registers them,
  and only steps with no external effect exist: `record_finding` (highest level
  L0, writes a finding to the ops record) and `draft_reply` (L1, stores a draft,
  never sends). An action class whose mediator is a step may not have a
  `max_level` above the step's. An action that changes an external system
  (a ticket update, a restart) must be a live tool of a tool pack; an
  automation never calls an external system any other way (ADR 0002 section 7.2).
- **Proposal schema.** The proposal is the `ops_proposal` record of
  `ops-record`. The schema must be a valid JSON Schema and must require
  `summary`, `evidence_ids`, `action_class`, `payload`, `confidence` and
  `rollback` with these types: `summary`, `action_class` and `rollback`
  strings; `evidence_ids` an array of strings; `payload` an object;
  `confidence` a number with `minimum` >= 0 and `maximum` <= 1. It may not
  name other properties, and may add constraints such as `minItems: 1` on the
  evidence. The runtime fills the other record fields (`incident_id`, mode,
  autonomy level, generation read) and serialises `payload` to JSON text,
  because the record stores it as text.
- **Budget.** `cost_per_run`, `cost_per_day` and `runs_per_hour` must be
  greater than zero, and a run may not cost more than a day.
- **Duplicate keys.** A key repeated in a manifest (the automation block
  included) or in the `action_classes` file is refused. YAML would otherwise
  keep the last one silently.

### Levels

Three places set a level for one automation, and none may name L3:

| Where | Key | Meaning |
|---|---|---|
| pack | `automation.default_level` | the level it starts at when the instance sets none |
| pack | `automation.max_level` | the most the pack allows; each action class also has a `max_level` |
| bundle | `automations: {<name>: {max_level}}` in `bundle.yaml` | an upper bound for the bundle's instances; `<name>` is the automation pack |
| instance | `automations: {<name>: {level}}` in `archi.yaml` | the level to run at |

The effective level is the lowest of the instance level (the pack default when
the instance sets none), the bundle bound and the pack maximum. When two
selected bundles bound the same automation, the lower bound counts.
`archi.ops.levels.effective_level(...)` computes it and raises `LevelError`
for L3 or an unknown value in any input, so the runtime can call it at run
start. `archi bundles validate`, loading `archi.yaml` and `effective_level`
all refuse any value other than `L0`, `L1` or `L2`, citing ADR 0002 D13.

### What the renderer writes

For each selected automation pack, `archi render` writes
`<instance>/automations/<name>.yaml` and records the same declaration in
`archi.lock` under `composed.automations`. It holds the trigger, context gate,
proposal schema, budget, `default_level`, `pack_max_level`, `bundle_max_level`
(the lowest bound any selected bundle sets, or `null`), the playbook as its
path in the rendered profile (`skills/<playbook>`) and the action class ids.
The file also carries the action classes in full
(`action_class_definitions`); the lock keeps those once, under
`composed.action_classes`. The instance's own level is not rendered: it is set
in `archi.yaml`. `archi add` of an automation pack re-renders both.

`archi bundles validate` also refuses: a missing `automation:` block (or one on a
pack of another kind), a missing playbook file, an action class id that is
repeated, listed twice or not defined, an invalid `payload_schema`, a mediator
that resolves to neither a declared live tool nor a registered step, a bundle
bound for a pack that is not an automation pack, and a missing or mistyped
proposal field.
