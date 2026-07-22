# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A take-home PoC that bridges Terraform-managed identity resources (AWS IAM roles/users/keys) with a mock "Oasis" identity platform, so every non-human identity has a known origin, owner, and lifecycle state. Not production code — see "Known limitations" in [README.md](README.md) for the intentional gaps.

Two integration hooks share the same parser/translator:
- **Sync** (`oasis_bridge/sync_cli.py`): detective, zero-touch. Diffs Terraform *state* → lifecycle events (create/update/destroy) → upserts to Oasis. Two baselines: two consecutive state files (`--old`/`--new`), or — omitting `--old` — the current state against the live Oasis inventory (so no prior state file is retained).
- **Gate** (`oasis_bridge/gate_cli.py`): preventive, opt-in CI step between `terraform plan` and `apply`. Sends proposed *plan* changes to Oasis for a policy verdict; exits non-zero on deny so `apply` never runs.

## Commands

This is a [uv](https://docs.astral.sh/uv/) project (Python 3.13, pinned via `.python-version`); dependencies are locked in `uv.lock`.

```bash
uv sync                # create .venv and install deps (+ dev group: pytest)

# Full demo: starts mock Oasis API + runs all 3 scenarios (sync, clean gate, denied gate)
./run_demo.sh

# Run pieces individually — start the mock API first:
uv run uvicorn mock_oasis.server:app --port 8080 &

uv run oasis-sync --old samples/state_v1.json --new samples/state_v2.json
uv run oasis-sync --old samples/state_v1.json --new samples/state_v2.json --dry-run  # no server needed

uv run oasis-gate --plan samples/plan_approved.json   # exit 0
uv run oasis-gate --plan samples/plan_denied.json     # exit 1

# Offline tests (no server, no network) — the whole suite, always run this to verify changes
uv run pytest
# or, for a single test:
uv run pytest tests/test_bridge.py::test_gate_denies_admin_and_long_lived_key -v
```

`oasis-sync` and `oasis-gate` are console-script entry points defined in `pyproject.toml` (`[project.scripts]`), mapped to `sync_cli:main` / `gate_cli:main`. There is no build/lint step configured (no linter config, no CI beyond the illustrative workflow in `.github/workflows/terraform-oasis.yml`, which isn't itself runnable — it documents where the hooks go in a real pipeline).

## Architecture

Data flows in one direction through four modules in `oasis_bridge/`, all sharing the dataclasses in `models.py`:

1. **`tf_parser.py`** — parses `terraform show -json` output. `parse_state()` and `parse_plan()` read the *same* JSON schema (Terraform emits it identically for state and plan), which is why one parser serves both hooks. Resources are keyed by their stable TF `address` (not ARN, since ARNs are "known after apply" and only exist in state).
2. **`differ.py`** — two entrypoints. `diff_states` set-diffs two parsed states by *address*: new-only → CREATE, old-only → DESTROY (sets `lifecycle_status: decommissioned`), present-in-both-but-changed → UPDATE. `diff_against_inventory` diffs a current state against the Oasis inventory keyed by *ARN* (`id`): in-state-not-in-inventory → CREATE, in-both-changed → UPDATE (incl. `oasis_discovery → terraform` reconciliation via `_inventory_delta`), and DESTROY only for `source: terraform` records that vanished (**source-scoped**, since one state isn't the whole inventory — see README limitations). Both detect privilege-increase policy attachments via the shared `_policy_delta_lists`/`_is_sensitive` helpers.
3. **`translator.py`** — maps a `TerraformResource` to the Oasis schema (`OasisIdentity`). This is where owner resolution happens (`resolve_owner`, precedence: `owner` tag → `team` tag → module path → `None`) and where `aws_iam_access_key` resources get folded into their parent `aws_iam_user` as `associated_secrets` rather than synced as standalone identities (`is_standalone_identity`).
4. **`oasis_client.py`** — thin `httpx` wrapper around the three mock API endpoints.

`sync_cli.py` and `gate_cli.py` are the two CLI entrypoints; they compose the modules above but contain no business logic themselves — that's deliberate, keep it that way.

`mock_oasis/` simulates the *server side* of the integration:
- `server.py` — FastAPI app with an in-memory `INVENTORY` dict, seeded with two identities that look "discovered" (`owner: None`, `source: "oasis_discovery"`) to demonstrate reconciliation when Terraform data lands on top of them. `reset_inventory()` restores the seed (used by tests/demo re-runs).
- `policy.py` — the plan-review rule engine (`RULES` list, each a pure function `change -> Violation | None`). Adding a policy rule means adding a function here and appending it to `RULES`. Any `high`-severity violation forces verdict `deny`; anything else with violations is `approve_with_warnings`.

**Extending to new resource types**: add the TF type to `IDENTITY_RESOURCE_TYPES` (and `SECRET_RESOURCE_TYPES` if it's a credential) in `models.py` — the parser, differ, and translator all key off that set.

**Fixtures as spec**: `samples/state_v1.json` / `state_v2.json` and `plan_approved.json` / `plan_denied.json` are hand-crafted `terraform show -json` fixtures that exercise every code path (all three lifecycle events, both gate verdicts). `tests/test_bridge.py` asserts against them directly — when changing differ/translator/policy behavior, check whether these fixtures need updating too.
