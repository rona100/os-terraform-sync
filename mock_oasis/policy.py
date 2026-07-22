"""Identity policy engine for the plan-time gate.

These rules live *inside* Oasis (server-side), not in the customer repo. That is
a deliberate design choice: the security team owns and versions the rules
centrally, and every pipeline gets the same governance without copying policy
into each codebase.

Each rule inspects a proposed plan change and may emit a PolicyViolation.
"""
from __future__ import annotations

from typing import Any, Callable

Violation = dict[str, str]


def _after(change: dict[str, Any]) -> dict[str, Any]:
    return change.get("after") or {}


def _policies(change: dict[str, Any]) -> list[str]:
    return _after(change).get("managed_policy_arns", []) or []


def _tags(change: dict[str, Any]) -> dict[str, str]:
    return _after(change).get("tags") or {}


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


RULES: list[Callable[[dict[str, Any]], Violation | None]] = [
    rule_no_admin_on_new_nhi,
    rule_no_long_lived_access_key,
    rule_require_owner_tag,
    rule_no_destroy_with_active_secrets,
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
