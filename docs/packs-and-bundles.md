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
| `sources` | context packs | Directory of templates, one `<source>.yaml` per source. Its single top-level key is `<source>`. |
| `schemas` | | Directory of LinkML schema slices. |
| `init_questions` | | File holding an `init_questions:` list, in okg's profile question format. |
| `invariants` | | File holding an `invariants:` list. The pack owns them. |
| `freshness` | | `{<pack>.<source>: {max_staleness: 24h}}`. Units: `s m h d w`. |
| `playbooks` | | Directory of playbooks. |
| `skill_triggers` | | File with `version:` and an `intents:` mapping. |
| `search_subtypes` | | Subtypes added to the `content` search profile. |
| `live_tools` | | `archi.tools.*` entries for the MCP server. |
| `action_classes` | | File holding an `action_classes:` mapping keyed by id (automation packs). |
| `files` | | Files installed at the same path in the deployment root (okg's `files:` allow-list applies). |
| `prompt_fragment` | | Markdown appended to the chat system prompt. |
| `credentials` | | Environment-variable **names** the pack reads. |

Source templates may reference two kinds of answers: the base questions
every instance has (`deployment_name`, `postgres_dsn`, `archi_data_root`,
`chat_site_name`, `chat_model`; see
[`base/questions.yaml`](../python/archi/bundles/base/questions.yaml)) and the
pack's own questions, by their local id (`${site_url}`).

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
  declares;
- a `freshness` key that is not one of the pack's own sources;
- a bundle that names an unknown pack or bundle, extends itself in a cycle,
  or has no credential-free default context pack (taking inherited defaults
  into account), because a bundle with nothing configured must publish.

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
| search `content` subtypes | union | — |
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
