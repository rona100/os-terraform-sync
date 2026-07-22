"""Shared data models for the Oasis <-> Terraform bridge.

These are deliberately small, plain dataclasses. They give the rest of the code
a typed vocabulary to pass around instead of loose dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# The Terraform resource types we parse. Identities (NHIs) plus the credential and
# rotation resources that hang off them.
# Extend this set to cover more providers (GCP service accounts, Azure MIs, etc.).
IDENTITY_RESOURCE_TYPES = {
    "aws_iam_role",
    "aws_iam_user",
    "aws_iam_access_key",
    "aws_iam_service_linked_role",
    "aws_secretsmanager_secret",
    "aws_secretsmanager_secret_rotation",
    # federation anchors (trust context, not identities themselves)
    "aws_iam_openid_connect_provider",
    "aws_iam_saml_provider",
    # multi-cloud NHIs
    "google_service_account",
    # "azurerm_user_assigned_identity",
}

# Types that represent a *credential/secret* rather than a standalone identity.
# These are folded into their owning identity as `associated_secrets`.
SECRET_RESOURCE_TYPES = {"aws_iam_access_key", "aws_secretsmanager_secret"}

# Rotation config attached to a secret -- metadata, never an identity of its own.
ROTATION_RESOURCE_TYPES = {"aws_secretsmanager_secret_rotation"}

# Federation trust anchors (OIDC/SAML). A workload assumes a role *through* these
# rather than holding a long-lived credential, so we parse them for context but
# never sync them as identities in their own right.
FEDERATION_RESOURCE_TYPES = {
    "aws_iam_openid_connect_provider",
    "aws_iam_saml_provider",
}


class LifecycleEvent(str, Enum):
    """How an identity changed between two Terraform states."""
    CREATE = "create"
    UPDATE = "update"
    DESTROY = "destroy"


class LifecycleStatus(str, Enum):
    """Where an identity sits in its lifecycle (ILM), creation -> deprecation.

    Two sources drive these transitions and neither is sufficient alone:

        active               Terraform declares it and it is in use
        deprecated           Oasis sees it stale, Terraform still declares it
        decommission_pending Terraform destroyed it -- awaiting scan confirmation
        decommissioned       a discovery scan confirmed it is gone (terminal)
        orphaned             Terraform disowned it but the scan still sees it live

    `decommission_pending` matters because Terraform's claim of deletion is not
    proof of deletion: a failed destroy, a `state rm`, or an out-of-band recreate
    leaves a usable principal behind. Only a scan can confirm absence.
    """
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DECOMMISSION_PENDING = "decommission_pending"
    DECOMMISSIONED = "decommissioned"
    ORPHANED = "orphaned"


class Verdict(str, Enum):
    """Result of a plan-time policy review."""
    APPROVE = "approve"
    APPROVE_WITH_WARNINGS = "approve_with_warnings"
    DENY = "deny"


@dataclass
class TerraformResource:
    """A single identity-relevant resource pulled out of Terraform state."""
    address: str                     # e.g. aws_iam_role.payment_processor  (stable across applies)
    tf_type: str                     # e.g. aws_iam_role
    tf_name: str                     # e.g. payment_processor
    identity_id: str                 # ARN / stable id used as the Oasis identity id
    display_name: str
    values: dict[str, Any] = field(default_factory=dict)


@dataclass
class OasisIdentity:
    """An identity record in the Oasis inventory schema.

    `last_used` / `is_stale` are *discovery-owned*: they are runtime facts Terraform
    cannot know, so the translator leaves them None and the Oasis side merges rather
    than overwrites (see mock_oasis/server.py::_merge).
    """
    id: str
    type: str
    name: str
    owner: Optional[str]
    source: str
    lifecycle_status: str
    attached_policies: list[str] = field(default_factory=list)
    associated_secrets: list[dict[str, Any]] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    created_at: Optional[str] = None
    classification: dict[str, Any] = field(default_factory=dict)
    last_used: Optional[str] = None       # discovery-owned
    is_stale: Optional[bool] = None       # discovery-owned (None = Terraform can't know)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "name": self.name,
            "owner": self.owner,
            "source": self.source,
            "lifecycle_status": self.lifecycle_status,
            "attached_policies": self.attached_policies,
            "associated_secrets": self.associated_secrets,
            "tags": self.tags,
            "created_at": self.created_at,
            "classification": self.classification,
            "last_used": self.last_used,
            "is_stale": self.is_stale,
        }


@dataclass
class IdentityChange:
    """One identity that changed between two states, ready to sync to Oasis."""
    event: LifecycleEvent
    identity: OasisIdentity
    notes: list[str] = field(default_factory=list)   # human-readable change notes


@dataclass
class PlanChange:
    """A proposed change extracted from a Terraform *plan* (pre-apply)."""
    address: str
    tf_type: str
    action: str                      # create | update | delete | replace | no-op
    after: dict[str, Any] = field(default_factory=dict)
    before: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyViolation:
    rule: str
    severity: str                    # low | medium | high
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "severity": self.severity, "message": self.message}
