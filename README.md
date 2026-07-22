# Oasis ⇄ Terraform Identity Bridge (PoC)

Bridges **Terraform-managed identity resources** (AWS IAM roles / users / access
keys) with the **Oasis identity platform** so every Non-Human Identity (NHI) has a
known **origin**, a known **owner**, and a known **lifecycle state** — from the
moment it is created, not whenever a scanner happens to find it.

> **Take-home PoC.** Not production code. It demonstrates the core concept against
> a *mock* Oasis REST API — no AWS credentials or real Oasis environment required.
> See [Known limitations](#known-limitations) for the intentional gaps.

---

## Contents

- [What this is](#what-this-is)
- [The problem it closes](#the-problem-it-closes)
- [How it works — a three-part loop](#how-it-works--a-three-part-loop)
- [Quick start](#quick-start)
- [Usage](#usage)
- [What the demo shows](#what-the-demo-shows)
- [Architecture](#architecture)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Known limitations](#known-limitations)

---

## What this is

When infrastructure is provisioned with Terraform, the IAM roles, users, and keys
it creates are *non-human identities*. An identity platform like Oasis wants an
inventory of every NHI with its owner and lifecycle — but discovery-based scanning
finds these identities **after** the fact, stripped of the context (who created it,
why, for which service) that only existed in Terraform.

This bridge closes that gap. It reads Terraform's own output (`terraform show
-json`) and feeds two integration points that share one parser and translator:

- a **sync** that reconciles the Oasis inventory with Terraform ground truth, and
- a **gate** that lets Oasis policy review identity changes *before* they apply.

Everything runs locally against a mock Oasis API so the whole flow is demonstrable
end to end in one command.

## The problem it closes

Terraform and an identity platform each hold half of an NHI's story and never
exchange it:

| | Terraform knows | Oasis (discovery) knows |
|---|---|---|
| origin / intent | ✅ who wrote it, which module, what for | ❌ |
| ownership | ✅ from tags / module / Git | ❌ `owner: null` |
| lifecycle events | ✅ create / update / destroy | ⚠️ inferred, delayed |
| runtime reality | ❌ | ✅ last used, staleness, secrets |

Discovery finds an IAM role *after* it exists, with `owner: null` and
`source: "oasis_discovery"`. The accountability and intent lived in Terraform and
were thrown away. **This bridge marries the two** — Terraform provenance lands on
top of the discovered record and reconciles it.

Concretely, the bridge closes all three gaps an identity platform has for
Terraform-managed NHIs:

| Gap | How the bridge fills it |
|---|---|
| **Ownership** | `resolve_owner`: `owner` tag → `team` tag → module path |
| **Classification** | `classify`: environment, criticality, data sensitivity, `privileged` (from the policies actually attached) and `credential_type` (from the trust policy) |
| **Lifecycle awareness** | create / update / **deprecate** / decommission events derived from state |

Ownership and classification flow *one way* (Terraform → Oasis). Runtime facts
(`last_used`, `is_stale`) flow the other way and are **never overwritten** by a sync —
the two systems own different halves of the record.

## How it works — a three-part loop

Terraform integration alone is not enough, and the design does not pretend otherwise.
Terraform knows **intent**; only a scan knows **reality**. Both write to the inventory:

```
plan ──► [ OASIS GATE ] ──► apply ──► [ OASIS SYNC ] ──┐
          approve / deny              record intent    │
          preventive, opt-in          detective        ▼
                                                 ( Oasis inventory )
                                                       ▲
                            [ DISCOVERY SCAN ] ────────┘
                             confirm reality, continuous
```

**Source-of-truth contract** — each side is authoritative for a different half, and
neither may overwrite the other's:

| | Authoritative for |
|---|---|
| **Terraform** (gate + sync) | intent, provenance, ownership, classification |
| **Discovery** (scan) | **existence**, usage / staleness, anything Terraform never touched |

Where they disagree, **the disagreement is the finding**: an identity Terraform
destroyed that the scanner can still see is **orphaned** — ungoverned but still usable,
the most dangerous state an NHI can be in.

**Sync** (`oasis-sync`) — *detective, zero-touch.* Diffs Terraform **state** →
derives create / update / destroy lifecycle events → reconciles Oasis. It supports
two baselines for the diff:

| Mode | Command | Baseline | Retains prior state? |
|---|---|---|---|
| Two-file | `--old prev.json --new cur.json` | the previous state file | yes |
| Inventory | `--new cur.json` | the **live Oasis inventory** | **no** |

In production the sync is triggered by the **state backend's write event** (an S3
notification or a Terraform Cloud webhook), so it needs **no HCL or pipeline
changes** and covers *every* apply — CI, local, or laptop. See
[docs/architecture.md](docs/architecture.md#sync-triggering--deployment) for how the
sync knows an apply ran and how it's deployed (e.g. as a Lambda).

**Gate** (`oasis-gate`) — *preventive, opt-in.* A CI step between `terraform plan`
and `apply` sends the *proposed* identity changes to Oasis for a policy verdict. On
a **deny** it exits non-zero, so `apply` never runs. Policies live centrally in
Oasis, not copied into every repo.

**Discovery** — *continuous, server-side.* Oasis's own scan of the cloud. It is not a
fallback for when the bridge fails; it is what makes the bridge's claims **true**. A
`terraform destroy` is a *claim* of deletion, so sync parks the identity at
`decommission_pending` and the next scan settles it — confirmed gone, or **orphaned**.
The Terraform bridge never triggers it, which is exactly what keeps the two sources
independent.

Together they give a full ILM arc:

| Status | Set by | Meaning |
|---|---|---|
| `active` | sync | declared in Terraform, in use |
| `deprecated` | sync **+** discovery | stale in Oasis but still coded — cleanup candidate |
| `decommission_pending` | sync | Terraform destroyed it; awaiting confirmation |
| `decommissioned` | discovery | scan confirmed it is gone (terminal) |
| `orphaned` | discovery | Terraform disowned it but it is **still live** ⚠ |

The gate and sync share the same parser and schema-translation logic; they differ only
in whether they read *state* or *plan*, and whether they can *block*. See
[docs/flow-overview.md](docs/flow-overview.md) for sequence diagrams of each.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) (Python 3.13 is pinned via
`.python-version`; `uv` installs it if needed).

```bash
uv sync                 # create .venv and install deps
./run_demo.sh           # start the mock Oasis API and run all four scenarios
```

`run_demo.sh` is the fastest way to see everything: it boots the mock API, runs
both sync modes and both gate verdicts, then shuts the API down.

## Usage

To run the pieces individually, start the mock API first (the sync/gate CLIs talk
to it over HTTP; override the URL with `--api-url` or `OASIS_API_URL`):

```bash
uv run uvicorn mock_oasis.server:app --port 8080 &
```

**Sync — two-file diff** (baseline is the previous state file):

```bash
uv run oasis-sync --old samples/state_v1.json --new samples/state_v2.json
uv run oasis-sync --old samples/state_v1.json --new samples/state_v2.json --dry-run  # no server needed
```

**Sync — inventory diff** (baseline is the live Oasis inventory; no `--old`):

```bash
uv run oasis-sync --new samples/state_v2.json            # diff against current inventory
uv run oasis-sync --new samples/state_v2.json --dry-run  # read inventory, print changes, no upsert
```

**Gate — policy review** (exit code gates `terraform apply`):

```bash
uv run oasis-gate --plan samples/plan_approved.json   # clean plan  -> exit 0 (apply allowed)
uv run oasis-gate --plan samples/plan_denied.json     # risky plan  -> exit 1 (apply blocked)
```

## What the demo shows

The mock Oasis inventory is **seeded** with two roles Oasis discovered on its own
(`owner: null`, `source: oasis_discovery`) — mirroring the assignment's record. The
four `run_demo.sh` scenarios:

1. **Sync (two-file).** Diffs `state_v1 → state_v2` directly:
   - **CREATE** `github-actions-deployer` → a **workload identity**: assumed via OIDC
     (`credential_type: federated`) with *no* associated secret — the deliberate
     contrast with `batch-runner`, which is `static_key` with a long-lived key.
   - **CREATE** `invoice-sync` → registered from Terraform with a resolved owner.
   - **DESTROY** `report-generator` → `lifecycle_status: active → decommissioned`.
   - **UPDATE** `payment-processor` → matches the *discovered* record and reconciles
     `source: oasis_discovery → terraform`, `owner: null → payments-team@…`, and
     flags a **privilege increase** (`+AdministratorAccess`).
2. **Sync (inventory).** Runs twice against the Oasis inventory to show steady-state
   incremental sync *and the full ILM arc*:
   - Run 1 stamps records `source: terraform` and adds **classification**; it also
     flags `report-generator` **`active → deprecated`** — Oasis had observed it stale
     (`is_stale: true`, unused since 2024) while Terraform still declares it. Neither
     system can conclude that alone; only the bridge can.
   - Run 2 sees it disappear from state and moves it
     **`deprecated → decommission_pending`** (source-scoped — see
     [Known limitations](#known-limitations)). Note it stops at *pending*: Terraform's
     word alone isn't enough to retire an identity.

   Discovery's runtime fields (`last_used`, `is_stale`) **survive** the sync — the
   Oasis side merges rather than overwrites, since Terraform can't observe usage.
3. **Discovery scan.** `report-generator` sits at `decommission_pending` after
   Terraform destroyed it. Two scans show why that matters: the first still finds the
   role in the cloud → **`orphaned`** ⚠ (a failed destroy or `state rm` left a usable
   credential behind); a later scan no longer sees it → **`decommissioned`** ✅.
4. **Gate (clean plan).** A well-formed plan → **APPROVE**, exit 0.
5. **Gate (risky plan).** An admin-on-new-role plus a long-lived key → **DENY**,
   exit 1, so `apply` is blocked.

## Architecture

Data flows in **one direction** through four modules in `oasis_bridge/`, all
sharing the dataclasses in `models.py`:

```
terraform show -json
        │
        ▼
   tf_parser ──► differ ──► translator ──► oasis_client ──► Oasis API
   (parse)      (state      (TF resource    (HTTP)          (mock_oasis/)
                 diff ->     -> Oasis
                 events)     identity)
```

- **`tf_parser.py`** parses state *and* plan (Terraform emits the same JSON schema
  for both).
- **`differ.py`** turns two states — or one state and the Oasis inventory — into
  lifecycle events.
- **`translator.py`** maps a Terraform resource to the Oasis schema, resolving
  ownership and folding access keys into their user as secrets.
- **`oasis_client.py`** is a thin HTTP client for the three Oasis endpoints.
- `sync_cli.py` / `gate_cli.py` are the two entrypoints; they only compose the
  modules above — no business logic.

`mock_oasis/` simulates the server side: an in-memory inventory + a plan-review
policy engine.

For the full walkthrough — module responsibilities, the data model, the two sync
baselines, and how to extend it — see **[docs/architecture.md](docs/architecture.md)**.
For sequence diagrams of each hook, see
**[docs/flow-overview.md](docs/flow-overview.md)**.

## Testing

Offline unit tests — no server, no network — cover the differ (both modes),
ownership resolution, and both gate verdicts against the sample fixtures:

```bash
uv run pytest
uv run pytest tests/test_bridge.py -v                 # verbose
```

## Project layout

```
oasis_bridge/         the integration logic
  tf_parser.py        parse terraform show -json (state AND plan)
  differ.py           diff state -> lifecycle events (two-file OR vs. inventory)
  translator.py       map TF resource -> Oasis schema; resolve ownership
  oasis_client.py     thin HTTP client for the Oasis REST API
  sync_cli.py         `oasis-sync`  (detective / zero-touch)
  gate_cli.py         `oasis-gate`  (preventive / opt-in)
mock_oasis/
  server.py           mock Oasis REST API (inventory + plan-review), seeded
  policy.py           central identity policy rules (server-side)
samples/              state_v1/v2 and approved/denied plan fixtures
tests/                offline unit tests
docs/                 architecture notes + flow diagrams (Mermaid)
.github/workflows/    example CI placement of both hooks
run_demo.sh           one-command end-to-end demo
```

## Known limitations

Intentional gaps for a PoC — and good discussion starters. For a deeper, design-level
self-critique (coverage boundary, ordering/consistency, reconciliation semantics, the
privilege model), see **[docs/design-review.md](docs/design-review.md)**.

- **Detective, not preventive** for the state hook: state is written *after* apply,
  so the sync records reality; it can't block it. The gate adds prevention, but only
  for identities that go through CI.
- **Plan-time unknowns:** ARNs are "known after apply", so the authoritative
  inventory write happens against state, not the plan.
- **Ownership conflicts:** tag vs. CODEOWNERS vs. Git author can disagree; precedence
  here is tag → team → module → none. Git provenance needs a CI hook.
- **Out-of-band changes bypass the bridge — discovery is the backstop.** The sync
  only sees what lands in Terraform *state*, so any identity change that doesn't flow
  through a normal `terraform apply` is invisible to it: a role/user created or edited
  straight in the **AWS console, CLI, or SDK**; **drift** (a policy attached out of
  band that state won't reflect until the next `apply`/refresh); **another IaC tool**
  (CloudFormation, Pulumi) managing the identity; or **`terraform state rm`**, which
  drops a resource from state *without* deleting it in AWS — so the next diff reads as
  a **false DESTROY** even though the identity is still live.
  **"Discovery"** is Oasis's own capability that scans the cloud provider directly
  (reading AWS IAM, etc.) *independent of Terraform* — the same mechanism that seeds
  the mock inventory with `source: "oasis_discovery"`, `owner: null` records (it can
  see that an identity *exists*, but not who owns it or why; see
  [The problem it closes](#the-problem-it-closes)). Because it observes cloud truth
  rather than Terraform state, discovery catches exactly what this bridge structurally
  cannot, so nothing that exists in the cloud stays invisible. The two are
  complementary layers: the bridge adds provenance/ownership for Terraform-managed
  identities, and discovery is the catch-all safety net. Where they disagree (e.g.
  `state rm`), a real integration trusts cloud-truth for *existence* and Terraform for
  *provenance*.
- **Inventory-mode destroys are source-scoped (not workspace-scoped).** When the
  baseline is the Oasis inventory (no `--old` file), a single state is *not* the whole
  inventory — it also holds `oasis_discovery` records and other workspaces' resources.
  So a DESTROY is only inferred for records already stamped `source: terraform` that
  vanish from the current state; discovery/other-tool records are never touched. The
  gap: with multiple states writing to one inventory, absence from *one* state can't
  distinguish "destroyed here" from "lives in another workspace." The proper fix is to
  stamp each record with its originating workspace/state key and scope destroy-inference
  to that key — the two-file `--old` diff sidesteps this since both files are one root
  module.
- **Secrets in state:** state can contain sensitive values; a real integration reads it
  with least privilege and never persists raw secret material.
```
