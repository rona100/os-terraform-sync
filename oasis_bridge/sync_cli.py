"""oasis-sync: diff two Terraform states and reconcile the Oasis inventory.

Usage:
    python -m oasis_bridge.sync_cli --old samples/state_v1.json --new samples/state_v2.json

This is the zero-touch, detective integration: point it at two consecutive
state versions (in production, triggered by an S3/GCS/TFC state-write event) and
it derives lifecycle events and syncs them to Oasis. No customer HCL or pipeline
changes required.
"""
from __future__ import annotations

import argparse
import sys

from .differ import diff_against_inventory, diff_states
from .oasis_client import OasisClient
from .tf_parser import load_json

# ANSI colors for a readable demo
DIM = "\033[2m"; BOLD = "\033[1m"; RESET = "\033[0m"
GREEN = "\033[32m"; RED = "\033[31m"; YELLOW = "\033[33m"; CYAN = "\033[36m"

EVENT_STYLE = {
    "create": (GREEN, "CREATE "),
    "update": (YELLOW, "UPDATE "),
    "destroy": (RED, "DESTROY"),
}


def _print_inventory(title: str, identities: list[dict]) -> None:
    print(f"\n{BOLD}{title}{RESET}")
    print(f"{DIM}{'NAME':<22}{'OWNER':<34}{'SOURCE':<18}{'STATUS'}{RESET}")
    for i in sorted(identities, key=lambda x: x["name"]):
        owner = i["owner"] if i["owner"] is not None else f"{RED}null{RESET}     "
        source = i["source"]
        src_col = f"{RED}{source}{RESET}" if source == "oasis_discovery" else f"{GREEN}{source}{RESET}"
        print(f"{i['name']:<22}{_pad(owner, 34)}{_pad(src_col, 18)}{i['lifecycle_status']}")


def _pad(colored: str, width: int) -> str:
    """Pad a possibly-colored string to a visible width."""
    import re
    visible = re.sub(r"\033\[[0-9;]*m", "", colored)
    return colored + " " * max(0, width - len(visible))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sync Terraform identity changes to Oasis.")
    ap.add_argument(
        "--old",
        default=None,
        help="previous Terraform state JSON; omit to diff against the Oasis inventory",
    )
    ap.add_argument("--new", required=True, help="current Terraform state JSON")
    ap.add_argument("--api-url", default=None, help="Oasis API base URL")
    ap.add_argument("--dry-run", action="store_true", help="print changes without upserting to Oasis")
    args = ap.parse_args(argv)

    new_state = load_json(args.new)

    # Two-file mode diffs two states offline; inventory mode uses the live Oasis
    # inventory as the baseline (so no prior state file has to be retained).
    client = OasisClient(base_url=args.api_url) if args.api_url else OasisClient()
    try:
        if args.old is not None:
            changes = diff_states(load_json(args.old), new_state)
        else:
            changes = diff_against_inventory(new_state, client.list_identities())

        print(f"\n{BOLD}{CYAN}== Terraform state diff -> identity lifecycle events =={RESET}")
        if not changes:
            print("No identity changes detected.")
            return 0

        for ch in changes:
            color, label = EVENT_STYLE[ch.event.value]
            print(f"  {color}{label}{RESET}  {BOLD}{ch.identity.name}{RESET}  {DIM}({ch.identity.id}){RESET}")
            for note in ch.notes:
                print(f"           {DIM}- {note}{RESET}")

        if args.dry_run:
            print(f"\n{DIM}(dry-run: not upserting to Oasis){RESET}")
            return 0

        _print_inventory("OASIS INVENTORY  (before sync)", client.list_identities())

        payload = [
            {"event": ch.event.value, "identity": ch.identity.to_dict(), "notes": ch.notes}
            for ch in changes
        ]
        result = client.sync_identities(payload)

        print(f"\n{BOLD}{CYAN}== Reconciliation report =={RESET}")
        for item in result["report"]:
            known = "matched discovered record" if item["was_known"] else "new to Oasis"
            print(f"  {BOLD}{item['name']}{RESET}  {DIM}[{item['event']}, {known}]{RESET}")
            for fc in item["field_changes"]:
                arrow = f"{fc['from']} {DIM}->{RESET} {GREEN}{fc['to']}{RESET}"
                print(f"      {fc['field']:<18}: {arrow}")

        _print_inventory("OASIS INVENTORY  (after sync)", client.list_identities())
    finally:
        client.close()

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
