"""A mock of the Oasis Platform REST API.

Just enough surface to demonstrate the integration end to end:

  GET  /api/v1/identities            -> current inventory
  POST /api/v1/identities:sync       -> upsert identities, return reconciliation
  POST /api/v1/terraform/plan-review -> central policy verdict for a plan

The inventory is seeded with ONE identity that Oasis "discovered" on its own,
with owner=null and source=oasis_discovery -- mirroring the record in the
assignment. When the Terraform sync runs, watch that record get reconciled.
"""
from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from .discovery import scan
from .policy import evaluate

app = FastAPI(title="Mock Oasis Platform", version="0.1.0")


# --- seed: what discovery found before the integration existed ------------
def _seed() -> dict[str, dict[str, Any]]:
    """Discovery-only inventory: identities found by scanning, with no provenance.

    `last_used` / `is_stale` are the runtime facts only discovery can see -- they are
    what the Terraform sync must *preserve* rather than overwrite, and what makes
    deprecation detectable (report-generator is stale but still coded).
    """
    return {
        "arn:aws:iam::123456789012:role/payment-processor": {
            "id": "arn:aws:iam::123456789012:role/payment-processor",
            "type": "aws_iam_role",
            "name": "payment-processor",
            "owner": None,                     # <-- discovery can't attribute ownership
            "source": "oasis_discovery",       # <-- found by scanning, not by Terraform
            "lifecycle_status": "active",
            "attached_policies": [
                "arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess"
            ],
            "associated_secrets": [],
            "tags": {},
            "created_at": "2024-01-15T10:00:00Z",
            "classification": {},              # <-- discovery can't classify either
            "trust": [],                       # <-- Terraform enriches: who may assume it
            "last_used": "2025-11-01T08:00:00Z",
            "is_stale": False,
        },
        "arn:aws:iam::123456789012:role/report-generator": {
            "id": "arn:aws:iam::123456789012:role/report-generator",
            "type": "aws_iam_role",
            "name": "report-generator",
            "owner": None,
            "source": "oasis_discovery",
            "lifecycle_status": "active",
            "attached_policies": [
                "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
            ],
            "associated_secrets": [],
            "tags": {},
            "created_at": "2024-06-02T09:30:00Z",
            "classification": {},
            "trust": [],
            "last_used": "2024-08-10T04:15:00Z",   # <-- not used in over a year
            "is_stale": True,                      # <-- deprecation candidate
        },
    }


INVENTORY: dict[str, dict[str, Any]] = _seed()


class SyncRequest(BaseModel):
    changes: list[dict[str, Any]]


class PlanReviewRequest(BaseModel):
    run_id: str | None = None
    actor: str | None = None
    changes: list[dict[str, Any]]


class ScanRequest(BaseModel):
    """What a discovery scan actually observed alive in the cloud."""
    live_identities: list[str] = []


@app.get("/api/v1/identities")
def list_identities() -> dict[str, Any]:
    return {"identities": list(INVENTORY.values())}


@app.post("/api/v1/identities:sync")
def sync_identities(req: SyncRequest) -> dict[str, Any]:
    """Upsert identity changes and return a field-level reconciliation report."""
    report: list[dict[str, Any]] = []

    for change in req.changes:
        event = change["event"]
        identity = change["identity"]
        notes = change.get("notes", [])
        ident_id = identity["id"]

        before = copy.deepcopy(INVENTORY.get(ident_id))
        field_diffs = _reconcile(before, identity)

        # Upsert the enriched record for every event. On destroy the incoming
        # identity already carries lifecycle_status="decommissioned", so we keep
        # the record (enriched with owner/source/provenance) as an audit trail
        # rather than deleting it outright.
        #
        # MERGE, never replace: Terraform is authoritative for provenance
        # (owner/source/classification/policies) but knows nothing about runtime
        # usage, so discovery's fields must survive the write.
        INVENTORY[ident_id] = _merge(before, identity)

        report.append(
            {
                "id": ident_id,
                "name": identity["name"],
                "event": event,
                "was_known": before is not None,
                "previous_source": before["source"] if before else None,
                "field_changes": field_diffs,
                "notes": notes,
            }
        )

    return {"reconciled": len(report), "report": report}


@app.post("/api/v1/discovery:scan")
def discovery_scan(req: ScanRequest) -> dict[str, Any]:
    """Run a discovery scan and confirm (or contradict) pending decommissions.

    `live_identities` is what the scanner actually observed in the cloud. This is
    Oasis's own capability -- the Terraform bridge never calls it, which is what
    keeps the two sources of truth independent.
    """
    transitions = scan(INVENTORY, req.live_identities)
    return {
        "scanned": len(INVENTORY),
        "observed_live": len(req.live_identities),
        "transitions": transitions,
    }


@app.post("/api/v1/_reset")
def reset() -> dict[str, Any]:
    """Restore the seeded discovery-only inventory (demo/test convenience)."""
    reset_inventory()
    return {"reset": True, "identities": len(INVENTORY)}


@app.post("/api/v1/terraform/plan-review")
def plan_review(req: PlanReviewRequest) -> dict[str, Any]:
    verdict, violations = evaluate(req.changes)
    return {
        "run_id": req.run_id,
        "actor": req.actor,
        "verdict": verdict,
        "violations": violations,
        "evaluated": len(req.changes),
    }


# --- helpers --------------------------------------------------------------
_TRACKED_FIELDS = [
    "owner", "source", "lifecycle_status", "attached_policies", "classification", "trust",
]

# Fields only *discovery* can observe (runtime facts). Terraform sends them as None;
# a sync must never null them out.
DISCOVERY_OWNED_FIELDS = ("last_used", "is_stale")


def _merge(before: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Overlay Terraform's view onto the existing record without losing runtime data.

    Terraform owns provenance (owner, source, classification, policies, lifecycle);
    discovery owns `last_used` / `is_stale`. Incoming Nones for discovery-owned
    fields are dropped rather than written, so enrichment never destroys what
    scanning learned.
    """
    if before is None:
        return dict(incoming)
    merged = dict(before)
    for key, value in incoming.items():
        if key in DISCOVERY_OWNED_FIELDS and value is None:
            continue
        merged[key] = value
    return merged


def _reconcile(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict]:
    """Produce a human-readable field-level diff for the reconciliation report."""
    diffs: list[dict] = []
    for field in _TRACKED_FIELDS:
        old_v = before.get(field) if before else None
        new_v = after.get(field)
        if old_v != new_v:
            diffs.append({"field": field, "from": old_v, "to": new_v})
    return diffs


def reset_inventory() -> None:
    """Test/demo helper: restore the seeded discovery-only inventory."""
    global INVENTORY
    INVENTORY = _seed()
