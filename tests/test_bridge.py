"""Fast, offline tests for the core logic (no server required).

Run with:  python -m tests.test_bridge   (or: pytest)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oasis_bridge.differ import diff_against_inventory, diff_states
from oasis_bridge.models import LifecycleEvent
from oasis_bridge.tf_parser import load_json, parse_plan, parse_state
from oasis_bridge.translator import (
    classify,
    credential_type,
    is_standalone_identity,
    resolve_owner,
    to_oasis_identity,
)
from oasis_bridge.models import TerraformResource
from mock_oasis.discovery import scan
from mock_oasis.policy import evaluate
from mock_oasis.server import _merge

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _inventory_record(**overrides):
    """A minimal Oasis inventory record; override the fields a test cares about."""
    rec = {
        "id": "arn:aws:iam::123456789012:role/placeholder",
        "type": "aws_iam_role",
        "name": "placeholder",
        "owner": None,
        "source": "oasis_discovery",
        "lifecycle_status": "active",
        "attached_policies": [],
        "associated_secrets": [],
        "tags": {},
        "created_at": None,
        "classification": {},
        "last_used": None,
        "is_stale": False,
    }
    rec.update(overrides)
    return rec


def test_diff_detects_all_three_event_types():
    old = load_json(str(SAMPLES / "state_v1.json"))
    new = load_json(str(SAMPLES / "state_v2.json"))
    changes = {c.identity.name: c.event for c in diff_states(old, new)}

    assert changes["invoice-sync"] == LifecycleEvent.CREATE
    assert changes["report-generator"] == LifecycleEvent.DESTROY
    assert changes["payment-processor"] == LifecycleEvent.UPDATE
    # access keys are folded into their user, not surfaced as standalone changes
    assert "batch-runner" not in changes or changes["batch-runner"] != LifecycleEvent.CREATE


def test_destroy_is_pending_until_a_scan_confirms():
    # Terraform saying it's gone is a claim, not proof -- it must not land as
    # `decommissioned` until discovery confirms the principal really disappeared.
    old = load_json(str(SAMPLES / "state_v1.json"))
    new = load_json(str(SAMPLES / "state_v2.json"))
    destroyed = [c for c in diff_states(old, new) if c.event == LifecycleEvent.DESTROY][0]
    assert destroyed.identity.lifecycle_status == "decommission_pending"


def test_ownership_resolution_precedence():
    # explicit owner tag wins
    r = TerraformResource("aws_iam_role.x", "aws_iam_role", "x", "arn", "x",
                          {"tags": {"owner": "a@b.com", "team": "t"}})
    assert resolve_owner(r) == "a@b.com"
    # team tag is the fallback
    r2 = TerraformResource("aws_iam_role.x", "aws_iam_role", "x", "arn", "x",
                           {"tags": {"team": "payments"}})
    assert resolve_owner(r2) == "team:payments"
    # module path is the structural fallback
    r3 = TerraformResource("module.billing.aws_iam_role.x", "aws_iam_role", "x", "arn", "x", {})
    assert resolve_owner(r3) == "module:module.billing"
    # nothing -> None (flag as needs-owner)
    r4 = TerraformResource("aws_iam_role.x", "aws_iam_role", "x", "arn", "x", {})
    assert resolve_owner(r4) is None


PAYMENT_ARN = "arn:aws:iam::123456789012:role/payment-processor"
REPORT_ARN = "arn:aws:iam::123456789012:role/report-generator"


def test_inventory_diff_reconciles_discovered_record():
    # payment-processor exists in Oasis as a discovery-only record; the current
    # state carries Terraform provenance and an added AdministratorAccess policy.
    new = load_json(str(SAMPLES / "state_v2.json"))
    inventory = [
        _inventory_record(
            id=PAYMENT_ARN,
            name="payment-processor",
            source="oasis_discovery",
            attached_policies=["arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess"],
        )
    ]
    changes = {c.identity.id: c for c in diff_against_inventory(new, inventory)}

    upd = changes[PAYMENT_ARN]
    assert upd.event == LifecycleEvent.UPDATE
    assert any("source: oasis_discovery -> terraform" in n for n in upd.notes)
    assert any("AdministratorAccess" in n and "PRIVILEGE INCREASE" in n for n in upd.notes)


def test_inventory_diff_creates_unknown_identity():
    new = load_json(str(SAMPLES / "state_v2.json"))
    changes = {c.identity.name: c.event for c in diff_against_inventory(new, [])}
    assert changes["invoice-sync"] == LifecycleEvent.CREATE


def test_inventory_diff_destroys_terraform_record_absent_from_state():
    # A terraform-sourced record no longer present in the current state -> DESTROY.
    new = load_json(str(SAMPLES / "state_v2.json"))
    inventory = [_inventory_record(id=REPORT_ARN, name="report-generator", source="terraform")]
    changes = {c.identity.id: c for c in diff_against_inventory(new, inventory)}

    destroyed = changes[REPORT_ARN]
    assert destroyed.event == LifecycleEvent.DESTROY
    assert destroyed.identity.lifecycle_status == "decommission_pending"


def test_retired_records_are_not_re_destroyed():
    # Once Terraform has disowned an identity, later syncs must not keep emitting
    # DESTROY for it -- otherwise a pending/orphaned record never settles.
    new = load_json(str(SAMPLES / "state_v2.json"))
    for status in ("decommission_pending", "orphaned", "decommissioned"):
        inventory = [_inventory_record(id=REPORT_ARN, name="report-generator",
                                       source="terraform", lifecycle_status=status)]
        ids = {c.identity.id for c in diff_against_inventory(new, inventory)}
        assert REPORT_ARN not in ids, f"re-emitted DESTROY for a {status} record"


def test_scan_confirms_a_real_deletion():
    inv = {REPORT_ARN: _inventory_record(id=REPORT_ARN, name="report-generator",
                                         lifecycle_status="decommission_pending")}
    report = scan(inv, live_identities=[])          # scanner no longer sees it
    assert inv[REPORT_ARN]["lifecycle_status"] == "decommissioned"
    assert report[0]["from"] == "decommission_pending"
    assert report[0]["to"] == "decommissioned"


def test_scan_flags_an_orphan_when_the_principal_survives():
    # The dangerous case: Terraform thinks it destroyed the role, but it is still
    # live in the cloud (failed destroy / state rm) -- ungoverned and usable.
    inv = {REPORT_ARN: _inventory_record(id=REPORT_ARN, name="report-generator",
                                         lifecycle_status="decommission_pending")}
    scan(inv, live_identities=[REPORT_ARN])
    assert inv[REPORT_ARN]["lifecycle_status"] == "orphaned"

    # ...and an orphan resolves once a later scan no longer sees it
    scan(inv, live_identities=[])
    assert inv[REPORT_ARN]["lifecycle_status"] == "decommissioned"


def test_scan_leaves_active_records_alone():
    inv = {PAYMENT_ARN: _inventory_record(id=PAYMENT_ARN, name="payment-processor",
                                          lifecycle_status="active")}
    assert scan(inv, live_identities=[]) == []      # absence != decommission here
    assert inv[PAYMENT_ARN]["lifecycle_status"] == "active"


def test_inventory_diff_leaves_discovery_records_untouched():
    # Same record but discovery-sourced: absence must NOT trigger a destroy.
    new = load_json(str(SAMPLES / "state_v2.json"))
    inventory = [_inventory_record(id=REPORT_ARN, name="report-generator", source="oasis_discovery")]
    changed_ids = {c.identity.id for c in diff_against_inventory(new, inventory)}
    assert REPORT_ARN not in changed_ids


def test_classification_precedence_and_privilege():
    # explicit tags win
    r = TerraformResource("aws_iam_role.x", "aws_iam_role", "x", "arn", "x",
                          {"tags": {"environment": "prod", "criticality": "low",
                                    "data_classification": "pii"}})
    c = classify(r)
    assert c["environment"] == "prod"
    assert c["criticality"] == "low"          # explicit tag beats the privilege default
    assert c["data_sensitivity"] == "pii"
    assert c["privileged"] is False

    # privilege is derived from the attached policies, and implies high criticality
    r2 = TerraformResource("aws_iam_role.y", "aws_iam_role", "y", "arn", "y",
                           {"managed_policy_arns": ["arn:aws:iam::aws:policy/AdministratorAccess"]})
    c2 = classify(r2)
    assert c2["privileged"] is True
    assert c2["criticality"] == "high"

    # module path is the structural fallback for environment
    r3 = TerraformResource("module.staging_billing.aws_iam_role.z", "aws_iam_role",
                           "z", "arn", "z", {})
    assert classify(r3)["environment"] == "staging"


def test_secrets_use_the_oasis_schema_and_pick_up_rotation():
    state = parse_state(load_json(str(SAMPLES / "state_v2.json")))
    by_name = {r.values.get("name"): r for r in state.values()}

    # a Secrets Manager secret associates to a *role* and picks up its rotation config
    role = by_name["payment-processor"]
    secrets = to_oasis_identity(role, state).associated_secrets
    assert len(secrets) == 1
    secret = secrets[0]
    assert secret["store"] == "aws_secrets_manager"
    assert secret["rotation_enabled"] is True
    assert secret["rotation_period_days"] == 90
    # rotation timestamps are discovery-owned -- Terraform must not claim them
    assert secret["last_rotated_at"] is None
    assert secret["next_rotation_at"] is None

    # a long-lived IAM access key reports no rotation, in the same schema
    key = to_oasis_identity(by_name["batch-runner"], state).associated_secrets[0]
    assert key["store"] == "aws_iam"
    assert key["rotation_enabled"] is False
    assert {"id", "store", "rotation_enabled", "last_rotated_at", "next_rotation_at"} <= key.keys()


def test_translator_does_not_claim_runtime_fields():
    state = parse_state(load_json(str(SAMPLES / "state_v2.json")))
    role = next(r for r in state.values() if r.values.get("name") == "payment-processor")
    ident = to_oasis_identity(role, state)
    assert ident.last_used is None and ident.is_stale is None


def test_sync_merge_preserves_discovery_owned_fields():
    before = {
        "id": PAYMENT_ARN, "owner": None, "source": "oasis_discovery",
        "last_used": "2025-11-01T08:00:00Z", "is_stale": True,
    }
    incoming = {
        "id": PAYMENT_ARN, "owner": "payments-team@company.com", "source": "terraform",
        "last_used": None, "is_stale": None,      # Terraform can't know these
    }
    merged = _merge(before, incoming)
    assert merged["owner"] == "payments-team@company.com"   # Terraform wins provenance
    assert merged["source"] == "terraform"
    assert merged["last_used"] == "2025-11-01T08:00:00Z"    # discovery data survives
    assert merged["is_stale"] is True


def test_stale_but_still_coded_identity_is_deprecated():
    # Oasis says stale; Terraform still declares it -> neither source knows this alone.
    new = load_json(str(SAMPLES / "state_v2.json"))
    inventory = [
        _inventory_record(id=PAYMENT_ARN, name="payment-processor", source="terraform",
                          is_stale=True, last_used="2024-08-10T04:15:00Z")
    ]
    changes = {c.identity.id: c for c in diff_against_inventory(new, inventory)}
    upd = changes[PAYMENT_ARN]
    assert upd.identity.lifecycle_status == "deprecated"
    assert any("deprecation candidate" in n for n in upd.notes)


def _trust_policy(principal: dict) -> str:
    return json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": principal,
                       "Action": "sts:AssumeRole"}],
    })


def test_workload_identity_is_recognised_from_the_trust_policy():
    state = parse_state(load_json(str(SAMPLES / "state_v2.json")))
    by_name = {r.values.get("name"): r for r in state.values()}

    # federated (OIDC) role: a workload identity -- no standing credential
    deployer = to_oasis_identity(by_name["github-actions-deployer"], state)
    assert deployer.classification["credential_type"] == "federated"
    assert deployer.associated_secrets == []

    # the deliberate contrast: an IAM user fronted by a long-lived key
    runner = to_oasis_identity(by_name["batch-runner"], state)
    assert runner.classification["credential_type"] == "static_key"
    assert len(runner.associated_secrets) == 1


def test_credential_type_variants():
    svc = TerraformResource("aws_iam_role.svc", "aws_iam_role", "svc", "arn", "svc",
                            {"assume_role_policy": _trust_policy({"Service": "lambda.amazonaws.com"})})
    assert credential_type(svc) == "service"

    acct = TerraformResource("aws_iam_role.a", "aws_iam_role", "a", "arn", "a",
                             {"assume_role_policy": _trust_policy({"AWS": "arn:aws:iam::123456789012:root"})})
    assert credential_type(acct) == "assumed_role"

    # a trust policy we can't parse must degrade quietly, never raise
    broken = TerraformResource("aws_iam_role.b", "aws_iam_role", "b", "arn", "b",
                               {"assume_role_policy": "{not json"})
    assert credential_type(broken) is None


def test_gcp_service_account_translates():
    sa = TerraformResource(
        "google_service_account.pipeline", "google_service_account", "pipeline",
        "data-pipeline@acme.iam.gserviceaccount.com", "data-pipeline",
        {"email": "data-pipeline@acme.iam.gserviceaccount.com",
         "tags": {"owner": "data-team@company.com"}},
    )
    assert is_standalone_identity(sa)
    ident = to_oasis_identity(sa, {sa.address: sa})
    assert ident.owner == "data-team@company.com"
    assert ident.classification["credential_type"] == "service_account"


def test_federation_anchors_are_not_identities():
    state = parse_state(load_json(str(SAMPLES / "state_v2.json")))
    oidc = next(r for r in state.values()
                if r.tf_type == "aws_iam_openid_connect_provider")
    assert not is_standalone_identity(oidc)   # trust context, not an NHI


def test_gate_denies_admin_and_long_lived_key():
    plan = load_json(str(SAMPLES / "plan_denied.json"))
    changes = [
        {"address": c.address, "tf_type": c.tf_type, "action": c.action, "after": c.after}
        for c in parse_plan(plan)
    ]
    verdict, violations = evaluate(changes)
    rules = {v["rule"] for v in violations}
    assert verdict == "deny"
    assert "no-admin-on-new-nhi" in rules
    assert "no-long-lived-access-key" in rules


def test_gate_approves_clean_plan():
    plan = load_json(str(SAMPLES / "plan_approved.json"))
    changes = [
        {"address": c.address, "tf_type": c.tf_type, "action": c.action, "after": c.after}
        for c in parse_plan(plan)
    ]
    verdict, violations = evaluate(changes)
    assert verdict == "approve"
    assert violations == []


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS  {name}")
            passed += 1
    print(f"\n{passed} tests passed.")
