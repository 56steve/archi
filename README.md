<p align="center">
  <img src="https://raw.githubusercontent.com/archi-physics/archi/main/docs/docs/_static/archi-logo.png" width="200" />
</p>

# Archi — the HEP distribution of OKG

[![Docs](https://img.shields.io/badge/docs-online-blue)](https://archi-physics.github.io/archi/)

Archi is the HEP distribution of OKG: connectors, enrichers, live tools,
playbooks, schemas, and bundles, shipped as the pip wheel `archi` built from
[`python/`](python/). It contains no credentials, no site config, and no
running services — it is installed onto an OKG deployment via bundles
(`archi render --bundle cern-team`, then `okg install`). Instances live in their own
repositories and carry only configuration and secrets.

## Start here

```bash
archi init myteam && cd myteam && archi up && archi doctor
```

[Quickstart](docs/quickstart.md): install, the four commands, and what each
one does.

## Repository layout

- [`python/`](python/) — the `archi` package (wheel source, tests).
- [`bundles/`](bundles/) — bundles (e.g. [`cern-team`](bundles/cern-team/)) and the
  [packs](bundles/packs/) they select; see [packs and bundles](docs/packs-and-bundles.md).
- [`skills/`](skills/) — playbooks for agents operating over the graph.
- [`docs/`](docs/) — program docs and the MkDocs site.
- [`pact/`](pact/) — PACT change ledger (requirements, tasks, evidence).

## Key documents

- [Program spec (ADR 0001)](docs/adr/0001-archi-v3-program-spec.md) — the
  Archi v3 program: architecture, decisions, waves.
- [OKG alignment page](docs/okg-alignment.md) — the OKG-side sync page:
  current state, consumed substrate surface, issue mapping.
- [cern-team quickstart](docs/cern-team-quickstart.md) — install the cern-team
  bundle end to end. [The earlier install demo](docs/cern-team-demo.md) records
  the pre-pack layout.

## v2

The v2 application (the RAG chatbot) lives on the `main` branch and serves
existing deployments until cutover; this branch line carries v3 only.

## License

Archi is released under the [MIT License](LICENSE). For project inquiries,
contact paus@mit.edu.
