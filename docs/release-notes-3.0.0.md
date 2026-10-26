# archi 3.0.0

Archi 3.0 turns a facility's own knowledge (documentation, meetings, wikis,
tickets, code, catalogs) into a graph that an agent can read and cite, and
adds an operations layer that proposes and records actions. The package is
`archi-kb` on PyPI. You import it as `archi`.

3.0 is a new product, not an upgrade of v2. v2 lives on in the `v2` branch
and its `v2.x` tags.

## What you get

- **One command from an empty machine to a cited answer.** `archi init`,
  `archi up`, `archi doctor`. The graph survives `archi down` and `archi up`.
  A nightly build proves the path on an empty runner.
- **Bundles and packs.** Pick what goes in the graph. Shipped bundles:
  `cern-team` (documentation, meetings, wiki, documents, tickets, code) and
  `cms-physics` (physics TWiki, CDS, CMSSW releases, analysis notes and the
  analysis ids that link them). Add a source later with `archi add <pack>`.
  Write your own pack in a separate pip package.
- **A context layer that reports its own health.** Each pack declares how stale
  its sources may be, and which links and checks must hold. `archi doctor`
  reports them. The `ops-record` pack keeps incidents, proposals, decisions,
  actions and outcomes as records.
- **An operations layer.** Automations run at levels L0 to L2 (L3 is refused),
  start in shadow, and are promoted by an approved decision. `archi ops
  status`, `pending`, `approve`, `reject`, `promote`, `halt`, `resume`. The
  first automation, `instance-health`, watches the instance itself.
- **Bring your own agent.** `archi connect` writes the MCP configuration for
  Claude Code, codex, openclaw, Open WebUI and generic MCP over HTTP.
- **Rewritten documentation**, with pages for every bundle, pack and command
  generated from the code.

## What is not in 3.0.0

- `archi eval` and context evals. A gate that needs a context-eval score fails,
  so the automation stays in shadow.
- The `cms-comp-ops` and `cluster-manager` bundles, and the `ticket-triage`
  automation with its shadow report. The v2 queue bots keep serving on a
  tagged `v2.x`.
- The `cms-physics` install proof in CI and the rebuild of the cms-kb instance
  from it.
- Level L3. Document upload in the chat UI. Redmine, SSO-page and Helm support.

The full list, and what each missing piece means for a v2 deployment, is in
[Moving from v2 to 3.0](https://github.com/archi-physics/archi/blob/main/docs/docs/migration-from-v2.md). Every item is
tracked in the [changelog](https://github.com/archi-physics/archi/blob/main/CHANGELOG.md).

Built and tested against okg 0.2.0 (AKMON). The asks filed with AKMON for what
Archi works around are listed in the changelog.

## Install

You need `docker` or `podman` (with `compose`), Python 3.12 or newer, and
`git`. Use a virtual environment.

```bash
pip install "okg @ https://github.com/mitdbg/akmon/releases/download/v0.2.0/okg-0.2.0-py3-none-any.whl" archi-kb==3.0.0
```

`okg` is the graph engine. It is not on PyPI yet, so it comes from the AKMON
release. Do not `pip install archi`: that name belongs to an unrelated package.

## Quickstart

```bash
archi init myteam --bundle cern-team
cd myteam
archi up
archi doctor --json
```

Then ask a question in the chat that `archi up` printed, or connect an agent
with `archi connect claude-code`. The [quickstart](https://github.com/archi-physics/archi/blob/main/docs/docs/quickstart.md)
explains each command, including the check that an answer cites graph records.

## Upgrade from v2

There is no in-place upgrade. Back up v2, stand 3.0 up beside it under another
name, move each source, compare answers, and move your users when ready. v2 is
never touched, so you can stop at any step.

- [Moving from v2 to 3.0](https://github.com/archi-physics/archi/blob/main/docs/docs/migration-from-v2.md): what to back up,
  how to run both, how each source moves, what has no equivalent, how to roll
  back.
- If you run a v2 deployment, tell the maintainers. The
  [cutover runbook](https://github.com/archi-physics/archi/blob/main/docs/docs/operating/cutover.md) waits for every live
  deployment to have an owner and a decision.
