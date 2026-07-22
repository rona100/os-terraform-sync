"""oasis-gate: plan-time policy gate for CI/CD (the opt-in preventive layer).

Usage:
    python -m oasis_bridge.gate_cli --plan samples/plan_denied.json

Sits between `terraform plan` and `terraform apply`. Sends identity-relevant
proposed changes to Oasis, which returns a central policy verdict. On "deny" the
process exits non-zero, so `apply` never runs. Emits GitHub Actions annotations
so violations surface inline on the PR.
"""
from __future__ import annotations

import argparse
import os
import sys

from .oasis_client import OasisClient
from .tf_parser import load_json, parse_plan

BOLD = "\033[1m"; RESET = "\033[0m"; DIM = "\033[2m"
GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"

IN_GHA = os.environ.get("GITHUB_ACTIONS") == "true"


def _annotate(level: str, message: str) -> None:
    """Emit a GitHub Actions workflow annotation (shows inline on the PR)."""
    if IN_GHA:
        print(f"::{level}::{message}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Oasis identity policy gate for Terraform plans.")
    ap.add_argument("--plan", required=True, help="terraform show -json <plan> output")
    ap.add_argument("--api-url", default=None)
    ap.add_argument(
        "--fail-open",
        action="store_true",
        help="if Oasis is unreachable, allow apply (availability-first). Default is fail-closed.",
    )
    args = ap.parse_args(argv)

    plan = load_json(args.plan)
    changes = parse_plan(plan)

    payload = {
        "run_id": os.environ.get("GITHUB_RUN_ID", "local-run"),
        "actor": os.environ.get("GITHUB_ACTOR", os.environ.get("USER", "unknown")),
        "changes": [
            {"address": c.address, "tf_type": c.tf_type, "action": c.action, "after": c.after}
            for c in changes
        ],
    }

    print(f"\n{BOLD}Oasis identity gate{RESET}  {DIM}({len(changes)} identity change(s) in plan){RESET}")

    client = OasisClient(base_url=args.api_url) if args.api_url else OasisClient()
    try:
        result = client.review_plan(payload)
    except Exception as exc:  # noqa: BLE001 - demo: any transport error
        if args.fail_open:
            print(f"{YELLOW}Oasis unreachable ({exc}); failing open -> apply allowed.{RESET}")
            return 0
        print(f"{RED}Oasis unreachable ({exc}); failing closed -> apply blocked.{RESET}")
        return 1
    finally:
        client.close()

    verdict = result["verdict"]
    for v in result["violations"]:
        sev = v["severity"].upper()
        color = RED if v["severity"] == "high" else YELLOW
        print(f"  {color}[{sev}]{RESET} {v['rule']}: {v['message']}")
        _annotate("error" if v["severity"] == "high" else "warning",
                  f"{v['rule']}: {v['message']}")

    if verdict == "deny":
        print(f"\n{RED}{BOLD}VERDICT: DENY{RESET} - terraform apply blocked by Oasis policy.\n")
        return 1
    if verdict == "approve_with_warnings":
        print(f"\n{YELLOW}{BOLD}VERDICT: APPROVE (with warnings){RESET} - apply may proceed.\n")
        return 0
    print(f"\n{GREEN}{BOLD}VERDICT: APPROVE{RESET} - no identity policy violations.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
