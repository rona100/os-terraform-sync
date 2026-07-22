"""Translate Terraform resources into the Oasis identity schema.

This is where the integration earns its keep: it fills in the fields that
discovery leaves empty or wrong -- owner, source, lifecycle_status,
attached_policies, associated_secrets -- using the context Terraform has and
discovery does not.
"""
from __future__ import annotations

from typing import Optional

import json

from .models import (
    FEDERATION_RESOURCE_TYPES,
    ROTATION_RESOURCE_TYPES,
    SECRET_RESOURCE_TYPES,
    OasisIdentity,
    TerraformResource,
)

SOURCE_TERRAFORM = "terraform"

# Managed policies that make an identity privileged. Shared with the differ so the
# two agree on what "privilege increase" means. (mock_oasis/policy.py keeps its own
# copy on purpose -- server-side policy is owned by Oasis, not by this client.)
SENSITIVE_POLICIES = ("AdministratorAccess", "IAMFullAccess", "PowerUserAccess")

# Tokens we recognise in a module path when no environment tag is present.
_ENV_TOKENS = ("prod", "production", "staging", "stage", "dev", "development", "test")


def is_sensitive_policy(policy_arn: str) -> bool:
    """True if this managed policy confers broad/administrative privilege."""
    return any(s in policy_arn for s in SENSITIVE_POLICIES)


def resolve_owner(res: TerraformResource) -> Optional[str]:
    """Resolve an owner using a clear precedence order.

    1. explicit `owner` tag        (most intentional)
    2. `team` tag                  (team-level accountability)
    3. Terraform module path       (structural fallback)
    4. None                        (flag downstream as needs-owner)

    In a real integration you'd add Git provenance here (commit author,
    CODEOWNERS) captured by a CI hook -- Terraform state alone doesn't carry it.
    """
    tags = res.values.get("tags") or {}
    if tags.get("owner"):
        return tags["owner"]
    if tags.get("team"):
        return f"team:{tags['team']}"
    # module.<name>.<resource> -> attribute to the module
    if res.address.startswith("module."):
        module_path = ".".join(res.address.split(".")[:2])
        return f"module:{module_path}"
    return None


def _trust_principals(res: TerraformResource) -> list[dict]:
    """Pull the Principal blocks out of a role's trust policy.

    `assume_role_policy` arrives as a JSON *string* in state; `Statement` may be a
    single object or a list. Malformed/absent policies yield [] rather than raising --
    a trust policy we can't parse must not break a sync.
    """
    raw = res.values.get("assume_role_policy")
    if not raw:
        return []
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
    principals = []
    for stmt in statements:
        if isinstance(stmt, dict) and isinstance(stmt.get("Principal"), dict):
            principals.append(stmt["Principal"])
    return principals


def credential_type(
    res: TerraformResource,
    all_resources: Optional[dict[str, TerraformResource]] = None,
) -> Optional[str]:
    """How does this identity actually authenticate?

    This is the security-meaningful distinction behind "workload identity": an
    identity assumed through federation holds *no standing credential*, whereas one
    fronted by an access key does. Derived from the trust policy -- the very field
    the gate/differ otherwise ignore.

      federated    : trusted via an OIDC/SAML provider (GitHub Actions, IRSA, ...)
      service      : assumed by an AWS service (lambda.amazonaws.com, ...)
      static_key   : fronted by a long-lived access key
      assumed_role : trusted by an AWS account/principal
    """
    if res.tf_type == "google_service_account":
        return "service_account"

    for principal in _trust_principals(res):
        if principal.get("Federated"):
            return "federated"
    for principal in _trust_principals(res):
        if principal.get("Service"):
            return "service"

    if all_resources and any(
        other.tf_type == "aws_iam_access_key"
        and other.values.get("user") == res.values.get("name")
        for other in all_resources.values()
    ):
        return "static_key"

    for principal in _trust_principals(res):
        if principal.get("AWS"):
            return "assumed_role"
    return None


def classify(
    res: TerraformResource,
    all_resources: Optional[dict[str, TerraformResource]] = None,
) -> dict:
    """Derive an identity classification from Terraform signal.

    The assignment calls out "no classification" alongside missing ownership and
    lifecycle. Terraform carries that signal in tags, module structure, the policies
    actually attached, and the trust policy -- discovery does not. Precedence per field:

      environment      : `environment` tag -> `env` tag -> token in the module path
      criticality      : `criticality` tag -> `tier` tag -> "high" if privileged
      data_sensitivity : `data_classification` tag -> `data_class` tag
      privileged       : any attached managed policy is administrative
      credential_type  : from the trust policy / attached credentials
    """
    tags = res.values.get("tags") or {}

    environment = tags.get("environment") or tags.get("env") or _env_from_address(res.address)
    data_sensitivity = tags.get("data_classification") or tags.get("data_class")
    privileged = any(
        is_sensitive_policy(p) for p in (res.values.get("managed_policy_arns") or [])
    )
    criticality = tags.get("criticality") or tags.get("tier")
    if not criticality and privileged:
        criticality = "high"   # broad privilege implies high criticality

    return {
        "environment": environment,
        "criticality": criticality,
        "data_sensitivity": data_sensitivity,
        "privileged": privileged,
        "credential_type": credential_type(res, all_resources),
    }


def _env_from_address(address: str) -> Optional[str]:
    """Structural fallback: pick an environment token out of the module path."""
    lowered = address.lower()
    for token in _ENV_TOKENS:
        if f".{token}" in lowered or f"_{token}" in lowered or f"-{token}" in lowered:
            return token
    return None


def _rotation_for_secret(
    secret: TerraformResource,
    all_resources: dict[str, TerraformResource],
) -> tuple[bool, Optional[int]]:
    """Find the rotation config pointing at this secret, if any."""
    for other in all_resources.values():
        if other.tf_type not in ROTATION_RESOURCE_TYPES:
            continue
        target = other.values.get("secret_id")
        if target and target in (secret.identity_id, secret.values.get("arn"), secret.values.get("id")):
            rules = other.values.get("rotation_rules") or []
            if isinstance(rules, list):
                rules = rules[0] if rules else {}
            days = (rules or {}).get("automatically_after_days")
            return True, days
    return False, None


def _owns_secret(res: TerraformResource, secret: TerraformResource) -> bool:
    """Does this Secrets Manager secret belong to this identity?

    Precedence mirrors `resolve_owner`: an explicit tag wins, then the naming
    convention from the assignment's own example (secret `payment-processor-key`
    belongs to role `payment-processor`).
    """
    identity_name = res.values.get("name") or ""
    if not identity_name:
        return False
    tags = secret.values.get("tags") or {}
    tagged = tags.get("identity") or tags.get("nhi")
    if tagged:
        return tagged == identity_name
    secret_name = secret.values.get("name") or ""
    return secret_name.startswith(identity_name)


def _secrets_for_identity(
    res: TerraformResource,
    all_resources: dict[str, TerraformResource],
) -> list[dict]:
    """Collect credential resources that belong to this identity.

    Two kinds, both emitted in the Oasis `associated_secrets` shape:
      * aws_iam_access_key   -> matched to its IAM user (long-lived, no rotation)
      * aws_secretsmanager_secret -> matched to a role or user by tag / name prefix

    `last_rotated_at` / `next_rotation_at` are deliberately None: they are runtime
    facts only discovery can observe, so Terraform must not claim them.
    """
    secrets: list[dict] = []
    identity_name = res.values.get("name")

    for other in all_resources.values():
        if other.tf_type not in SECRET_RESOURCE_TYPES:
            continue

        if other.tf_type == "aws_iam_access_key":
            if res.tf_type != "aws_iam_user" or other.values.get("user") != identity_name:
                continue
            secrets.append(
                {
                    "id": other.identity_id,
                    "store": "aws_iam",
                    "type": "access_key",
                    "status": other.values.get("status", "Active"),
                    "created_at": other.values.get("create_date"),
                    "rotation_enabled": False,  # long-lived static key: no native rotation
                    "last_rotated_at": None,
                    "next_rotation_at": None,
                }
            )
        elif other.tf_type == "aws_secretsmanager_secret":
            if not _owns_secret(res, other):
                continue
            rotation_enabled, period_days = _rotation_for_secret(other, all_resources)
            secrets.append(
                {
                    "id": other.identity_id,
                    "store": "aws_secrets_manager",
                    "type": "secret",
                    "status": "Active",
                    "created_at": other.values.get("create_date"),
                    "rotation_enabled": rotation_enabled,
                    "rotation_period_days": period_days,
                    "last_rotated_at": None,
                    "next_rotation_at": None,
                }
            )

    return secrets


def to_oasis_identity(
    res: TerraformResource,
    all_resources: dict[str, TerraformResource],
    lifecycle_status: str = "active",
) -> OasisIdentity:
    """Build an Oasis identity record from a Terraform resource.

    Note what is *not* set: `last_used` / `is_stale` stay None because Terraform
    cannot observe runtime usage. Oasis merges those in from discovery.
    """
    return OasisIdentity(
        id=res.identity_id,
        type=res.tf_type,
        name=res.display_name,
        owner=resolve_owner(res),
        source=SOURCE_TERRAFORM,
        lifecycle_status=lifecycle_status,
        attached_policies=list(res.values.get("managed_policy_arns", [])),
        associated_secrets=_secrets_for_identity(res, all_resources),
        tags=res.values.get("tags") or {},
        created_at=res.values.get("create_date"),
        classification=classify(res, all_resources),
    )


def is_standalone_identity(res: TerraformResource) -> bool:
    """Only real NHIs sync on their own.

    Secrets and rotation config fold into their owning identity; federation anchors
    (OIDC/SAML providers) are trust context, not identities.
    """
    return (
        res.tf_type not in SECRET_RESOURCE_TYPES
        and res.tf_type not in ROTATION_RESOURCE_TYPES
        and res.tf_type not in FEDERATION_RESOURCE_TYPES
    )
