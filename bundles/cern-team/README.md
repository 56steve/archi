# cern-team bundle

A generic CERN team knowledge instance (ADR 0002 section 5). The bundle is
one manifest, [`bundle.yaml`](bundle.yaml): a selection of packs from
[`bundles/packs/`](../packs/). Each pack carries its own source templates,
schema slices, coverage invariant, playbooks and chat-prompt fragment, so a
pack you leave out leaves nothing behind in the instance.

| Packs | |
|---|---|
| default | `docsite` (documentation sites, from the static-docs cache), `indico` (Indico events, read live), `core-playbooks` (the generic retrieval playbooks) |
| optional | `twiki` (an EOS dump), `cds` (CERN Document Server over OAI), `jira` (the JIRA cache), `git-code` (one Git repository's files and history), `code-text` (function bodies), `code-symbols` (functions, classes, calls) |

`cmssw-releases` is no longer part of cern-team: it went to the CMS bundles.
Any pack, this one included, can still be added with `--with`.

## Install

```bash
archi render --bundle cern-team --with git-code --instance .   # ./profiles/cern-team
okg install --profile cern-team --deployment-name <slug> \
  --postgres-dsn "$OKG_DSN" --archi-data-root "$PWD/archi-data/data" \
  --indico-event-ids '["<event id>"]' \
  --git-code-repo-name <name> --git-code-repo-url <git url>
```

Each pack's questions become `--<pack>-<question>` flags. Then commit the
deployment directory, `okg ingest` and `okg run --once --apply`; the
[cern-team quickstart](../../docs/cern-team-quickstart.md) has every step.
`archi add <pack>` adds a pack to an installed instance.

Every selected source must be able to read its input, or the publish stops
for all of them. So select only what you can feed: docsite needs its records
cache (`python -m archi.downloaders.static_docs`), indico an event or
category, twiki a dump, jira its cache, git-code a repository URL. A bare
`archi render --bundle cern-team` selects docsite and indico; `--without`
drops either.

No file here or in the packs carries an operator path or a credential value:
paths are `${archi_data_root}`-relative or install answers, and credentials
are environment-variable names.

## Composition

`archi render` merges the selected packs into one okg profile (the rules are
in [packs and bundles](../../docs/packs-and-bundles.md)) and writes
`archi.lock` beside it. The shared HEP vocabulary
(`python/archi/schemas/`) ships in every pack that needs it as identical
files, so composing packs never conflicts on a schema.
