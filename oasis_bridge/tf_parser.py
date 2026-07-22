"""Parse Terraform `show -json` output.

Two entrypoints:
  * parse_state(...)  -> identity resources from a STATE file (post-apply ground truth)
  * parse_plan(...)   -> proposed changes from a PLAN file (pre-apply)

The same JSON schema is emitted by `terraform show -json <statefile>` and
`terraform show -json <planfile>`, so the parser we build for state transfers
almost unchanged to plans.
"""
from __future__ import annotations

import json
from typing import Any

from .models import (
    IDENTITY_RESOURCE_TYPES,
    PlanChange,
    TerraformResource,
)


def _iter_resources(module: dict[str, Any]):
    """Recursively yield every resource in a state module tree."""
    for res in module.get("resources", []):
        yield res
    for child in module.get("child_modules", []):
        yield from _iter_resources(child)


def _identity_id(res: dict[str, Any]) -> str:
    """Pick the stable identity id for a resource.

    Prefer ARN; fall back to id; finally the Terraform address. Real ARNs are
    only 'known after apply', which is exactly why the authoritative inventory
    write happens against STATE (post-apply) and not the plan.
    """
    values = res.get("values", {})
    return values.get("arn") or values.get("id") or res["address"]


def parse_state(state: dict[str, Any]) -> dict[str, TerraformResource]:
    """Return identity resources from a Terraform state, keyed by TF address.

    We key by `address` (stable across applies) so we can diff two states and
    tell create/update/destroy apart even when ARNs are assigned late.
    """
    root = state.get("values", {}).get("root_module", {})
    out: dict[str, TerraformResource] = {}
    for res in _iter_resources(root):
        if res.get("type") not in IDENTITY_RESOURCE_TYPES:
            continue
        values = res.get("values", {})
        out[res["address"]] = TerraformResource(
            address=res["address"],
            tf_type=res["type"],
            tf_name=res.get("name", ""),
            identity_id=_identity_id(res),
            display_name=values.get("name", res.get("name", "")),
            values=values,
        )
    return out


def parse_plan(plan: dict[str, Any]) -> list[PlanChange]:
    """Return identity-relevant proposed changes from a Terraform plan."""
    changes: list[PlanChange] = []
    for rc in plan.get("resource_changes", []):
        if rc.get("type") not in IDENTITY_RESOURCE_TYPES:
            continue
        change = rc.get("change", {})
        actions = change.get("actions", [])
        action = _normalize_actions(actions)
        if action == "no-op":
            continue
        changes.append(
            PlanChange(
                address=rc["address"],
                tf_type=rc["type"],
                action=action,
                after=change.get("after") or {},
                before=change.get("before") or {},
            )
        )
    return changes


def _normalize_actions(actions: list[str]) -> str:
    """Terraform encodes a replace as ['delete','create'] (or the reverse)."""
    a = set(actions)
    if a == {"create", "delete"} or a == {"delete", "create"}:
        return "replace"
    if actions == ["create"]:
        return "create"
    if actions == ["delete"]:
        return "delete"
    if actions == ["update"]:
        return "update"
    return actions[0] if actions else "no-op"


def load_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
