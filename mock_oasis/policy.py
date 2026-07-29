"""Identity policy engine for the plan-time gate.

These rules live *inside* Oasis (server-side), not in the customer repo. That is
a deliberate design choice: the security team owns and versions the rules
centrally, and every pipeline gets the same governance without copying policy
into each codebase.

Each rule inspects a proposed plan change and may emit a PolicyViolation.
"""
from __future__ import annotations

import json
from typing import Any, Callable

Violation = dict[str, str]


def _after(change: dict[str, Any]) -> dict[str, Any]:
    return change.get("after") or {}


def _policies(change: dict[str, Any]) -> list[str]:
    return _after(change).get("managed_policy_arns", []) or []


def _tags(change: dict[str, Any]) -> dict[str, str]:
    return _after(change).get("tags") or {}


# --- trust-policy parsing (server-side, deliberately independent of the client) ---
# The gate reads `assume_role_policy` straight out of the plan's `after` values. We keep
# our own tolerant parser here rather than importing oasis_bridge.translator, for the
# same reason SENSITIVE_POLICIES is duplicated below: server-side policy is owned by
# Oasis and must not depend on the customer's client code.
def _assume_principals(values: dict[str, Any]) -> list[tuple[str, Any, bool]]:
    """Yield (principal_type, principal_value, requires_external_id) for a trust doc.

    principal_type is one of wildcard | service | federated | aws. Malformed/absent
    policies yield [] -- an unparseable trust policy must not crash the gate.
    """
    raw = values.get("assume_role_policy")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, dict):
        return []
    statements = raw.get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]

    out: list[tuple[str, Any, bool]] = []
    for stmt in statements:
        if not isinstance(stmt, dict):
            continue
        ext_id = _has_external_id(stmt)
        principal = stmt.get("Principal")
        if principal == "*":
            out.append(("wildcard", "*", ext_id))
            continue
        if not isinstance(principal, dict):
            continue
        for kind, vals in principal.items():
            for value in vals if isinstance(vals, list) else [vals]:
                if value == "*":
                    out.append(("wildcard", "*", ext_id))
                elif kind == "Service":
                    out.append(("service", value, ext_id))
                elif kind == "Federated":
                    out.append(("federated", value, ext_id))
                else:
                    out.append(("aws", value, ext_id))
    return out


def _has_external_id(stmt: dict[str, Any]) -> bool:
    condition = stmt.get("Condition")
    if not isinstance(condition, dict):
        return False
    for operands in condition.values():
        if isinstance(operands, dict) and any(
            str(k).lower() == "sts:externalid" for k in operands
        ):
            return True
    return False


SENSITIVE_POLICIES = ("AdministratorAccess", "IAMFullAccess", "PowerUserAccess")


def rule_no_admin_on_new_nhi(change: dict[str, Any]) -> Violation | None:
    if change["action"] != "create" or change["tf_type"] != "aws_iam_role":
        return None
    for p in _policies(change):
        if any(s in p for s in SENSITIVE_POLICIES):
            return {
                "rule": "no-admin-on-new-nhi",
                "severity": "high",
                "message": (
                    f"New identity '{change['address']}' requests highly privileged "
                    f"policy {p.rsplit('/', 1)[-1]}."
                ),
            }
    return None


def rule_no_long_lived_access_key(change: dict[str, Any]) -> Violation | None:
    if change["action"] == "create" and change["tf_type"] == "aws_iam_access_key":
        return {
            "rule": "no-long-lived-access-key",
            "severity": "high",
            "message": (
                f"'{change['address']}' creates a long-lived static access key. "
                f"Prefer short-lived / role-based credentials."
            ),
        }
    return None


def rule_require_owner_tag(change: dict[str, Any]) -> Violation | None:
    # Access keys inherit ownership from their user, so we don't demand a tag on them.
    if change["action"] != "create" or change["tf_type"] not in ("aws_iam_role", "aws_iam_user"):
        return None
    tags = _tags(change)
    if not tags.get("owner") and not tags.get("team"):
        return {
            "rule": "require-owner-tag",
            "severity": "medium",
            "message": (
                f"New identity '{change['address']}' has no owner/team tag; it would "
                f"be created without accountable ownership."
            ),
        }
    return None


def rule_no_destroy_with_active_secrets(change: dict[str, Any]) -> Violation | None:
    # Destroying an identity that still fronts active credentials can orphan
    # those secrets in the secret store. (In a full build this would consult the
    # live Oasis inventory for associated_secrets; here we key off the plan.)
    if change["action"] in ("delete", "replace") and change["tf_type"] == "aws_iam_user":
        return {
            "rule": "no-destroy-with-active-secrets",
            "severity": "medium",
            "message": (
                f"'{change['address']}' is being destroyed; verify associated access "
                f"keys are revoked to avoid orphaned secrets."
            ),
        }
    return None


def rule_no_external_trust(change: dict[str, Any]) -> Violation | None:
    """Guard *who* may assume a role, not just what it can do.

    Fires on create AND update (attaching new trust to an existing role is an
    `update` -- exactly the escalation path the create-only admin rule misses):

      wildcard principal ("*")                 -> deny  (anyone can assume)
      AWS principal with no sts:ExternalId      -> warn  (confused-deputy risk)
      federated / service principals            -> allowed (sanctioned patterns)

    On update we only judge principals *newly added* vs `before`, so an unchanged
    trust policy never nags. Cross-account detection is shape-based: plan ARNs are
    "known after apply" (null), so we cannot compute true same-vs-cross account and
    instead flag the dangerous shapes (wildcard, unconstrained named AWS principal).
    """
    if change["action"] not in ("create", "update") or change["tf_type"] != "aws_iam_role":
        return None

    existing = set()
    if change["action"] == "update":
        existing = {(t, v) for (t, v, _) in _assume_principals(change.get("before") or {})}

    warning: Violation | None = None
    for ptype, value, has_external_id in _assume_principals(_after(change)):
        if (ptype, value) in existing:
            continue
        if ptype == "wildcard":
            return {
                "rule": "no-wildcard-trust",
                "severity": "high",
                "message": (
                    f"'{change['address']}' trusts a wildcard principal (\"*\") in its "
                    f"assume-role policy -- any principal could assume this role."
                ),
            }
        if ptype == "aws" and not has_external_id and warning is None:
            warning = {
                "rule": "no-unconstrained-cross-account-trust",
                "severity": "medium",
                "message": (
                    f"'{change['address']}' trusts AWS principal {value} with no "
                    f"sts:ExternalId condition (confused-deputy risk)."
                ),
            }
    return warning


RULES: list[Callable[[dict[str, Any]], Violation | None]] = [
    rule_no_admin_on_new_nhi,
    rule_no_long_lived_access_key,
    rule_require_owner_tag,
    rule_no_destroy_with_active_secrets,
    rule_no_external_trust,
]


def evaluate(changes: list[dict[str, Any]]) -> tuple[str, list[Violation]]:
    """Run every rule over every change. Return (verdict, violations)."""
    violations: list[Violation] = []
    for change in changes:
        for rule in RULES:
            v = rule(change)
            if v:
                violations.append(v)

    if any(v["severity"] == "high" for v in violations):
        verdict = "deny"
    elif violations:
        verdict = "approve_with_warnings"
    else:
        verdict = "approve"
    return verdict, violations
