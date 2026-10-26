# archi.yaml and ops.yaml

`archi init` writes `archi.yaml` into the instance. `archi up`, `down`,
`doctor` and `ops` read it. It holds settings, never secrets: those are in
`.env`.

## archi.yaml

| Key | Meaning |
|---|---|
| `instance_version` | The file format. Always `1`. |
| `name` | The instance and deployment name. A lowercase slug. |
| `bundles` | The bundles selected at init, in order. |
| `with_packs`, `without_packs` | Packs added to or removed from the bundles' defaults. |
| `profile` | The name of the rendered okg profile under `profiles/`. |
| `okg_version` | The OKG version the instance was made with. |
| `data_root` | Absolute path of the data root: connector caches and repository checkouts. Outside the instance. |
| `runtime` | `docker` or `podman`. |
| `ports` | Host ports, all bound to 127.0.0.1: `graph_db`, `mcp`, `webui`. |
| `admin_email` | The Open WebUI admin account. |
| `automations` | Per automation, `level`: `L0`, `L1` or `L2`. See below. |

```yaml
automations:
  instance-health:
    level: L1
```

A name under `automations` must be an automation pack the instance selected.
`level` can be `L0`, `L1` or `L2`. Any other value, including `L3`, is
refused when the file loads. The effective level is the lowest of this
value, the bundle's bound and the pack's maximum.

## ops.yaml

Per automation, under `automations.<name>`:

| Key | Meaning |
|---|---|
| `mode` | `shadow` (default) or `live`. `live` counts only after an approved promotion. |
| `timeout_seconds` | How long an L2 escalation waits for a person. Default 4 hours. |
| `estimate_cost` | Cost checked against the budget before each proposal. Default: the pack's `cost_per_run`. |
| `promotion` | The change `archi ops promote` opened. Do not edit by hand. |
| `proposer`, `outcome` | How proposals and outcomes are produced. See [Running automations](pack-format.md#running-automations-archi-ops). |

## Other files

| File | What it holds |
|---|---|
| `.env` | Generated secrets, readable only by you, ignored by git. |
| `archi.lock` | Bundle and pack versions and content digests. Committed. |
| `compose.yaml` | The stack. Generated. Edit `archi.yaml`, not this file. |
| `profiles/<name>/` | The rendered okg profile. |

The keys of `pack.yaml` and `bundle.yaml` are in the
[pack and bundle format](pack-format.md).
