# Adding a source

A source is one connector reading one system into the graph. Sources come
inside context packs, so adding a source means adding a pack.

## Add a shipped pack

1. Find the pack in the [library](library/index.md). Its page lists the
   questions it asks and the credential names it reads.
2. Add it to the running instance:

   ```bash
   archi add git-code --answer git_code_repo_url=https://github.com/example/repo.git
   ```

   `--answer ID=VALUE` answers one of the pack's questions. The id is the pack
   name, with dashes as underscores, then an underscore and the question id.
   `archi add` edits the registry, claims ownership of the new source,
   commits the instance repository, and publishes again.
3. Run `archi doctor`. The `source-freshness` check shows the new source's
   age against its limit.

To choose packs at the start, use `archi init --with <pack>`.

## Credentials

A pack names its credential variables. It never holds a value. Put the value
in the environment of the process that reads the source, or in a cookie file
whose path an init question asks for. Packs that need none, such as
`docsite`, publish with nothing configured.

## Connectors that read a cache

Some connectors read a cache that another tool writes, for example the CRIC
topology or the DBS dataset catalog. Each cache has a contract: where it
lives and what it holds. See [connector caches](reference/connector-caches.md).
A pack's `prepare:` step can write a cache before the first run, so a bare
install has its input.

## A source Archi does not ship

Write a pack. A pack can live in another pip package and register through the
`archi.packs` entry point. See [Contributing a pack](contributing-a-pack.md).
