# Contributing a pack

A pack can live in this repository under `bundles/packs/<name>/`, or in your
own pip package. Either way it is the same directory.

## Layout

```
my-pack/
  pack.yaml            the manifest
  questions.yaml       init questions
  sources/<source>.yaml  one source template per source
  schemas/             LinkML schema slices
  invariants.yaml      coverage checks the pack owns
  playbooks/           instructions for agents
  prompt.md            text added to the chat system prompt
```

The manifest has five required keys: `name` (equal to the directory name),
`kind`, `version`, `description` and `needs_credentials`. Everything else is
optional. The [format reference](reference/pack-format.md) lists every key.
Start from a small shipped pack and read its [library page](library/index.md).

## Rules that matter

- **No credentials in files.** List the environment variable names under
  `credentials`. A value in a manifest is refused.
- **Declare freshness for every source.** A context pack with sources needs
  one `max_staleness` for each.
- **Publish with nothing configured.** If your pack needs a credential, set
  `needs_credentials: true`. It then cannot be the only default pack of a
  bundle.
- **Own your invariants.** Add a coverage invariant for the records the pack
  should emit. It leaves with the pack.
- **Two packs may not ship different files at the same path.** The render
  stops and prints both digests.

## Check it

```bash
archi bundles validate path/to/my-pack
archi bundles list --pack-root path/to/packs
```

Then install it into a scratch instance and run `archi doctor`.

## Ship it from another package

Register the directory through an entry point, so `archi bundles list` finds
it with no flags:

```toml
[project.entry-points."archi.packs"]
mywg = "mywg_packs:packs_dir"      # returns a directory of <name>/pack.yaml

[project.entry-points."archi.bundles"]
mywg = "mywg_packs:bundles_dir"    # returns a directory of <name>/bundle.yaml
```

A broken package is reported and skipped. It does not hide the other packs.
The connector itself is an OKG `SourceAdapter` that your source template
names.

## Open a pull request

Follow the repository's contribution rules. Run `black --check .` and
`isort --check .` for formatting, and the tests in `python/tests`. Add or
update the docs for any behavior you change. The library page for your pack
is generated: run `cd python && python -m archi.docs_gen` and commit the
result. A test fails if you forget.
