# cms-physics bundle

A CMS physics knowledge instance (ADR 0002 section 5). It extends
[`cern-team`](../cern-team/) and adds the physics packs by default:

| Pack | What it brings |
|---|---|
| `twiki` | TWiki topics from an EOS dump. Install with `--twiki-physics-filter true` to keep physics topics only. |
| `cds` | CMS physics analysis summaries and notes from CERN Document Server |
| `cmssw-releases` | CMSSW releases |
| `analysis-notes` | Analysis notes from a mirror of `gitlab.cern.ch/tdr/notes` |
| `cms-analyses` | PAG and POG hubs, physics entities, and the rules linking analysis ids across the above |

## Install

```bash
archi render --bundle cms-physics --instance .
okg install --profile cms-physics --deployment-name <slug> \
  --postgres-dsn "$OKG_DSN" --archi-data-root "$PWD/archi-data/data" \
  --twiki-eos-root <TWiki dump> --twiki-physics-filter true \
  --analysis-notes-mirror-root <note mirror>
```

Add `PhysicsCrossLinker` to the deployment's `enrichers` block to turn on the
cross-link rules (see the [cms-analyses README](../packs/cms-analyses/README.md)).

## What needs credentials

The TWiki dump lives on CERN EOS and the note mirror is a clone of a CERN
GitLab group, so this bundle installs green only where both exist. The
[analysis-notes README](../packs/analysis-notes/README.md) has the mirror
commands. The bundle has no smoke eval yet, so it has no install proof of its
own; that proof needs both inputs.
