"""Diff two Terraform states into identity lifecycle events.

This is the core of the detective, zero-touch integration:

    appeared in new state  -> CREATE
    gone from new state    -> DESTROY  (lifecycle_status -> decommission_pending)
    present in both, changed -> UPDATE

Every `terraform apply` writes a new state version, so diffing consecutive
versions gives us lifecycle events for free -- no pipeline or HCL changes.

Note the DESTROY status: Terraform saying an identity is gone is *intent*, not
proof. A discovery scan (mock_oasis/discovery.py) confirms the principal really
disappeared before anything reaches `decommissioned` -- otherwise a failed destroy
or a `terraform state rm` would retire a credential that still works.
"""
from __future__ import annotations

from .models import (
    IdentityChange,
    LifecycleEvent,
    LifecycleStatus,
    OasisIdentity,
    TerraformResource,
)
from .translator import (
    SOURCE_TERRAFORM,
    is_sensitive_policy,
    is_standalone_identity,
    to_oasis_identity,
)

# Statuses that mean "Terraform has already disowned this" -- a later sync must not
# re-emit a DESTROY for them.
_ALREADY_RETIRED = {
    LifecycleStatus.DECOMMISSION_PENDING.value,
    LifecycleStatus.DECOMMISSIONED.value,
    LifecycleStatus.ORPHANED.value,
}


def _policy_delta_lists(old_policies: list[str], new_policies: list[str]) -> list[str]:
    """Human-readable attach/detach notes for two policy-ARN lists."""
    old_p = set(old_policies)
    new_p = set(new_policies)
    notes: list[str] = []
    for added in sorted(new_p - old_p):
        flag = "  [PRIVILEGE INCREASE]" if is_sensitive_policy(added) else ""
        notes.append(f"policy attached: +{_short(added)}{flag}")
    for removed in sorted(old_p - new_p):
        notes.append(f"policy detached: -{_short(removed)}")
    return notes


def _policy_delta(old: TerraformResource, new: TerraformResource) -> list[str]:
    return _policy_delta_lists(
        old.values.get("managed_policy_arns", []),
        new.values.get("managed_policy_arns", []),
    )


def _short(arn: str) -> str:
    return arn.rsplit("/", 1)[-1] if "/" in arn else arn


def diff_states(
    old_state: dict,
    new_state: dict,
) -> list[IdentityChange]:
    """Return the list of identity changes between two parsed states."""
    from .tf_parser import parse_state

    old = parse_state(old_state)
    new = parse_state(new_state)

    changes: list[IdentityChange] = []

    old_addrs = set(old)
    new_addrs = set(new)

    # CREATE: present in new, absent in old
    for addr in sorted(new_addrs - old_addrs):
        res = new[addr]
        if not is_standalone_identity(res):
            continue
        identity = to_oasis_identity(res, new, lifecycle_status=LifecycleStatus.ACTIVE.value)
        notes = [f"registered from Terraform (owner: {identity.owner})"]
        if identity.associated_secrets:
            notes.append(f"+{len(identity.associated_secrets)} associated secret(s)")
        changes.append(IdentityChange(LifecycleEvent.CREATE, identity, notes))

    # DESTROY: present in old, absent in new
    for addr in sorted(old_addrs - new_addrs):
        res = old[addr]
        if not is_standalone_identity(res):
            continue
        identity = to_oasis_identity(
            res, old, lifecycle_status=LifecycleStatus.DECOMMISSION_PENDING.value
        )
        changes.append(
            IdentityChange(
                LifecycleEvent.DESTROY,
                identity,
                [
                    "destroyed in Terraform",
                    "lifecycle_status -> decommission_pending",
                    "awaiting discovery scan to confirm the principal is really gone",
                ],
            )
        )

    # UPDATE: present in both, values differ
    for addr in sorted(new_addrs & old_addrs):
        old_res, new_res = old[addr], new[addr]
        if not is_standalone_identity(new_res):
            continue
        if old_res.values == new_res.values:
            continue
        identity = to_oasis_identity(new_res, new, lifecycle_status=LifecycleStatus.ACTIVE.value)
        notes = _policy_delta(old_res, new_res)
        if not notes:
            notes = ["attributes changed"]
        changes.append(IdentityChange(LifecycleEvent.UPDATE, identity, notes))

    return changes


def _inventory_delta(before: dict, after: OasisIdentity) -> list[str]:
    """Human-readable notes for how a Terraform record differs from Oasis's."""
    notes: list[str] = []

    old_source = before.get("source")
    if old_source != after.source:
        notes.append(f"source: {old_source} -> {after.source}")

    old_owner = before.get("owner")
    if old_owner != after.owner:
        notes.append(f"owner: {old_owner} -> {after.owner}")

    old_status = before.get("lifecycle_status")
    if old_status != after.lifecycle_status:
        notes.append(f"lifecycle_status: {old_status} -> {after.lifecycle_status}")

    old_class = before.get("classification") or {}
    if old_class != after.classification:
        notes.append(f"classification: {_fmt_class(old_class)} -> {_fmt_class(after.classification)}")

    notes.extend(
        _policy_delta_lists(before.get("attached_policies", []), after.attached_policies)
    )
    return notes


def _fmt_class(c: dict) -> str:
    """Compact one-line rendering of a classification dict."""
    if not c:
        return "none"
    parts = [f"{k}={v}" for k, v in sorted(c.items()) if v not in (None, False)]
    return "/".join(parts) if parts else "none"


def _identity_from_inventory(rec: dict, lifecycle_status: str) -> OasisIdentity:
    """Rebuild an OasisIdentity from an inventory record (for DESTROY payloads)."""
    return OasisIdentity(
        id=rec["id"],
        type=rec["type"],
        name=rec["name"],
        owner=rec.get("owner"),
        source=rec.get("source", SOURCE_TERRAFORM),
        lifecycle_status=lifecycle_status,
        attached_policies=list(rec.get("attached_policies", [])),
        associated_secrets=list(rec.get("associated_secrets", [])),
        tags=rec.get("tags") or {},
        created_at=rec.get("created_at"),
    )


def diff_against_inventory(
    new_state: dict,
    inventory: list[dict],
) -> list[IdentityChange]:
    """Diff a current TF state against the Oasis inventory (the last-known state).

    Unlike ``diff_states`` (which needs a prior state file), this uses the Oasis
    inventory itself as the baseline -- no "old" Terraform file to retain:

        in state, absent from inventory        -> CREATE
        in both, tracked fields differ         -> UPDATE  (incl. discovery->terraform)
        source=terraform record gone from state -> DESTROY (source-scoped)

    DESTROY is *source-scoped* on purpose: a single Terraform state is not the whole
    inventory (it also holds oasis_discovery records and other workspaces), so we only
    decommission records this bridge itself previously stamped ``source: terraform``.
    """
    from .tf_parser import parse_state

    new = parse_state(new_state)

    desired: dict[str, OasisIdentity] = {}
    for res in new.values():
        if not is_standalone_identity(res):
            continue
        ident = to_oasis_identity(res, new, lifecycle_status=LifecycleStatus.ACTIVE.value)
        desired[ident.id] = ident

    inv = {rec["id"]: rec for rec in inventory}
    changes: list[IdentityChange] = []

    # CREATE / UPDATE: everything Terraform currently declares
    for ident_id, ident in sorted(desired.items()):
        before = inv.get(ident_id)
        if before is None:
            notes = [f"registered from Terraform (owner: {ident.owner})"]
            if ident.associated_secrets:
                notes.append(f"+{len(ident.associated_secrets)} associated secret(s)")
            changes.append(IdentityChange(LifecycleEvent.CREATE, ident, notes))
        else:
            # ILM: Oasis observed it as stale, but Terraform still declares it.
            # Neither source can conclude this alone -- that is the whole point of
            # bridging them. Deprecated = "still coded, but unused: clean it up".
            deprecated = bool(before.get("is_stale")) and ident.lifecycle_status == LifecycleStatus.ACTIVE.value
            if deprecated:
                ident.lifecycle_status = LifecycleStatus.DEPRECATED.value
            notes = _inventory_delta(before, ident)
            if deprecated:
                notes.append(
                    f"stale in Oasis (last used: {before.get('last_used')}) but still "
                    f"declared in Terraform -> deprecation candidate"
                )
            if notes:
                changes.append(IdentityChange(LifecycleEvent.UPDATE, ident, notes))

    # DESTROY: terraform-sourced records that vanished from the current state
    for ident_id, rec in sorted(inv.items()):
        if ident_id in desired:
            continue
        if rec.get("source") != SOURCE_TERRAFORM:
            continue
        if rec.get("lifecycle_status") in _ALREADY_RETIRED:
            continue   # already pending/confirmed/orphaned -- don't re-emit
        identity = _identity_from_inventory(
            rec, LifecycleStatus.DECOMMISSION_PENDING.value
        )
        changes.append(
            IdentityChange(
                LifecycleEvent.DESTROY,
                identity,
                [
                    "absent from current Terraform state",
                    "lifecycle_status -> decommission_pending",
                    "awaiting discovery scan to confirm the principal is really gone",
                ],
            )
        )

    return changes
