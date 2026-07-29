# Flow overview

The bridge has two independent hooks into the Terraform lifecycle: a preventive
**gate** at plan-time and a detective **sync** at apply-time. Both share the same
parser (`oasis_bridge/tf_parser.py`) and talk to the same Oasis API, but they read
different artifacts (plan vs. state) and differ in whether they can block anything.

## High-level flow

```mermaid
flowchart LR
    A[terraform plan] --> B{Oasis Gate}
    B -- approve / approve_with_warnings --> C[terraform apply]
    B -- deny --> X[["apply blocked\n(exit 1)"]]
    C --> D[new state written]
    D --> E{Oasis Sync}
    E -- "intent: owner, class,<br/>lifecycle" --> F[(Oasis inventory)]
    B -. policy review .-> F
    G{{Discovery scan}} -- "reality: exists?<br/>last used?" --> F
    H[cloud provider] -- continuous scan --> G

    style X fill:#f8d7da,stroke:#c0392b
    style F fill:#d4edda,stroke:#2e7d32
    style G fill:#e0e7ff,stroke:#4338ca
```

Note the **two** writers into the inventory. Sync supplies what only Terraform knows
(intent, ownership, classification); discovery supplies what only a scan can know
(existence, usage). Neither overwrites the other's half — and where they disagree, the
disagreement is the finding.

- **Gate** (`oasis_bridge/gate_cli.py`) — opt-in CI step. Reviews the *proposed*
  plan against central Oasis policy (`mock_oasis/policy.py`) — not just *what* an
  identity can do (admin policies, long-lived keys) but *who may assume* it (a
  wildcard trust `Principal` is denied; an unconstrained cross-account one is
  warned), on update as well as create. Each change carries `before`/`after` so a
  trust rule flags only newly added principals. A `deny` verdict stops the pipeline
  before `apply` ever runs.
- **Discovery** (`mock_oasis/discovery.py`) — continuous, server-side. Scans the cloud
  for what actually exists. It confirms (or contradicts) what sync only *claims*: a
  destroyed identity sits at `decommission_pending` until a scan either confirms it is
  gone (`decommissioned`) or finds it still live (`orphaned`). The bridge never calls
  it — that independence is the point.
- **Sync** (`oasis_bridge/sync_cli.py`) — zero-touch. Diffs Terraform *state* and
  reconciles the Oasis inventory with the ground truth Terraform has (owner,
  source, lifecycle status). Runs after every apply, regardless of whether the gate
  was adopted. The baseline for the diff is either the previous state file
  (`diff_states`) or the live Oasis inventory (`diff_against_inventory`) — see
  [Variant: inventory-diff sync](#variant-inventory-diff-sync).

> The `State-write event` actor below is what triggers the sync in production (an
> S3 / GCS / TFC state-write event, or a manual run). For how that works and how the
> sync is deployed, see
> [Sync triggering & deployment](architecture.md#sync-triggering--deployment).

## Sequence: Gate (plan-time policy review)

```mermaid
sequenceDiagram
    actor Dev as Developer / CI
    participant TF as terraform plan
    participant Gate as gate_cli.main
    participant Parser as tf_parser.parse_plan
    participant Client as OasisClient
    participant API as Oasis API<br/>(mock_oasis/server.py)
    participant Policy as policy.evaluate

    Dev->>TF: terraform plan -out=tfplan
    TF-->>Dev: plan.json (terraform show -json)
    Dev->>Gate: oasis-gate --plan plan.json
    Gate->>Parser: parse_plan(plan)
    Parser-->>Gate: [PlanChange, ...] (identity-relevant only)
    Gate->>Client: review_plan({run_id, actor, changes})
    Client->>API: POST /api/v1/terraform/plan-review
    API->>Policy: evaluate(changes)
    loop each change x each rule
        Policy->>Policy: rule(change) -> Violation | None
    end
    Policy-->>API: (verdict, violations)
    API-->>Client: {verdict, violations}
    Client-->>Gate: result
    alt verdict == deny
        Gate-->>Dev: print violations, exit 1
        Note over Dev: terraform apply never runs
    else approve / approve_with_warnings
        Gate-->>Dev: print result, exit 0
        Dev->>Dev: terraform apply
    end
```

## Sequence: Sync (two-file state diff)

```mermaid
sequenceDiagram
    actor Trigger as State-write event<br/>(S3/TFC webhook, or manual)
    participant Sync as sync_cli.main
    participant Differ as differ.diff_states
    participant Parser as tf_parser.parse_state
    participant Trans as translator
    participant Client as OasisClient
    participant API as Oasis API<br/>(mock_oasis/server.py)

    Trigger->>Sync: oasis-sync --old state_v1.json --new state_v2.json
    Sync->>Differ: diff_states(old, new)
    Differ->>Parser: parse_state(old) / parse_state(new)
    Parser-->>Differ: {address: TerraformResource}
    Differ->>Differ: set-diff addresses -> create / destroy / update
    Differ->>Trans: to_oasis_identity(resource, ...)
    Trans-->>Differ: OasisIdentity (owner resolved, secrets folded in)
    Differ-->>Sync: [IdentityChange, ...]

    Sync->>Client: list_identities()
    Client->>API: GET /api/v1/identities
    API-->>Sync: inventory (before)

    Sync->>Client: sync_identities(changes)
    Client->>API: POST /api/v1/identities:sync
    API->>API: reconcile each identity,<br/>diff tracked fields, upsert INVENTORY
    API-->>Sync: reconciliation report (field-level changes)

    Sync->>Client: list_identities()
    Client->>API: GET /api/v1/identities
    API-->>Sync: inventory (after)
    Sync-->>Trigger: prints diff, reconciliation report, before/after inventory
```

## Variant: inventory-diff sync

Omit `--old` and the sync uses the **live Oasis inventory as the baseline** instead
of a previous state file — so nothing has to be retained between applies. The only
difference is the top of the diff: the "old" side is fetched over HTTP rather than
loaded from disk, and the diff keys by **ARN** (`id`) rather than TF address.

```mermaid
sequenceDiagram
    actor Trigger as State-write event<br/>(S3/TFC webhook, or manual)
    participant Sync as sync_cli.main
    participant Client as OasisClient
    participant API as Oasis API<br/>(mock_oasis/server.py)
    participant Differ as differ.diff_against_inventory
    participant Parser as tf_parser.parse_state

    Trigger->>Sync: oasis-sync --new state.json   (no --old)
    Sync->>Client: list_identities()
    Client->>API: GET /api/v1/identities
    API-->>Sync: inventory (the baseline)
    Sync->>Differ: diff_against_inventory(new_state, inventory)
    Differ->>Parser: parse_state(new)
    Parser-->>Differ: {address: TerraformResource}
    Differ->>Differ: key desired records by ARN, compare to inventory
    Note over Differ: in state, not in inventory: CREATE<br/>in both, fields differ: UPDATE<br/>source=terraform and gone: DESTROY (source-scoped)
    Differ-->>Sync: [IdentityChange, ...]
    Note over Sync,API: upsert + before/after inventory<br/>are identical to the two-file flow
```

Because a single state is not the whole inventory, DESTROY is **source-scoped**:
only records already stamped `source: terraform` are decommissioned when they
vanish — discovery and other-workspace records are left alone. See
[Known limitations](../README.md#known-limitations) and
[architecture.md](architecture.md#the-two-sync-baselines).

## Notes

- The gate and sync hooks are decoupled on purpose: a team can adopt the gate
  without the sync (or vice versa), and the sync remains the backstop for
  applies that never went through CI (local, laptop, other pipelines).
- `is_standalone_identity` means `aws_iam_access_key` resources never appear as
  their own `IdentityChange` — they're folded into their owning `aws_iam_user`
  as `associated_secrets` inside `translator.to_oasis_identity`.
- See [README.md](../README.md) for the conceptual gap this closes and known
  limitations, and [architecture.md](architecture.md) for the module-level
  walkthrough, data model, and the two sync baselines.
