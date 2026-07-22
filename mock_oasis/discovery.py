"""Oasis discovery: the scan that confirms what Terraform only *claims*.

Discovery is the third component of the integration, not a fallback. Terraform
knows *intent* (what should exist, who owns it, how it is classified); discovery
knows *reality* (what actually exists in the cloud right now, and how it is used).
Neither is sufficient alone:

  * Terraform destroying a resource is a claim of deletion, not proof of one. A
    failed destroy, a `terraform state rm`, or an out-of-band recreate leaves a
    live, usable principal behind. Sync therefore parks it at
    `decommission_pending` and this scan decides its fate.
  * Where the two sources disagree, the disagreement *is* the finding: an identity
    Terraform disowned that the scanner can still see is **orphaned** -- ungoverned
    but still usable, which is the most dangerous state an NHI can be in.

This module is deliberately server-side (Oasis's own capability). The Terraform
bridge never triggers it -- that is what keeps the two sources independent.
"""
from __future__ import annotations

from typing import Any, Iterable

from oasis_bridge.models import LifecycleStatus

# Records awaiting (or already failing) confirmation -- the only ones a scan judges.
_AWAITING_CONFIRMATION = {
    LifecycleStatus.DECOMMISSION_PENDING.value,
    LifecycleStatus.ORPHANED.value,
}


def scan(
    inventory: dict[str, dict[str, Any]],
    live_identities: Iterable[str],
) -> list[dict[str, Any]]:
    """Reconcile the inventory against what a cloud scan actually observed.

    `live_identities` is the set of principal ids the scanner found alive. Only
    records Terraform has disowned are judged; everything else is left alone,
    because this scan answers exactly one question -- does it still exist?

    Returns a per-identity transition report (mutates `inventory` in place).
    """
    live = set(live_identities)
    report: list[dict[str, Any]] = []

    for ident_id, record in inventory.items():
        before = record.get("lifecycle_status")
        if before not in _AWAITING_CONFIRMATION:
            continue

        if ident_id in live:
            after = LifecycleStatus.ORPHANED.value
            finding = (
                "still present in the cloud after Terraform destroyed it "
                "-- ungoverned and still usable"
            )
        else:
            after = LifecycleStatus.DECOMMISSIONED.value
            finding = "confirmed absent by scan -- decommission complete"

        record["lifecycle_status"] = after
        report.append(
            {
                "id": ident_id,
                "name": record.get("name"),
                "from": before,
                "to": after,
                "finding": finding,
            }
        )

    return report
