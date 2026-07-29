#!/usr/bin/env bash
#
# One-command end-to-end demo of the Oasis <-> Terraform bridge.
#
#   ./run_demo.sh
#
# Starts the mock Oasis API, then runs five scenarios:
#   1. State-diff sync (two files)   (the zero-touch, detective integration)
#   2. Inventory-diff sync           (baseline = Oasis inventory, no "old" file)
#   3. Discovery scan                (confirms deletion / flags orphans)
#   4. Policy gate on a clean plan   -> APPROVE (apply proceeds)
#   5. Policy gate on a risky plan   -> DENY    (apply blocked, exit 1)
#
set -euo pipefail
cd "$(dirname "$0")"

BOLD="\033[1m"; CYAN="\033[36m"; RESET="\033[0m"

hr() { printf "${CYAN}────────────────────────────────────────────────────────────${RESET}\n"; }
section() { echo; hr; printf "${BOLD}$1${RESET}\n"; hr; }

uv sync --quiet

echo "Starting mock Oasis Platform on http://127.0.0.1:8080 ..."
uv run uvicorn mock_oasis.server:app --host 127.0.0.1 --port 8080 --log-level warning &
OASIS_PID=$!
trap 'kill $OASIS_PID 2>/dev/null || true' EXIT

# wait for the API to accept connections
for _ in $(seq 1 20); do
  if curl -sf http://127.0.0.1:8080/api/v1/identities >/dev/null 2>&1; then break; fi
  sleep 0.5
done

section "SCENARIO 1  —  State-diff sync, two files (zero-touch / detective)"
echo "Diffing two consecutive Terraform state versions and reconciling Oasis."
uv run oasis-sync --old samples/state_v1.json --new samples/state_v2.json

section "SCENARIO 2  —  Inventory-diff sync (no 'old' state file retained)"
echo "Same detective sync, but the baseline is the Oasis inventory itself, so"
echo "only the current state is needed. Run twice to show steady-state incremental"
echo "sync: the first run stamps records source=terraform, the second sees one"
echo "disappear and decommissions it (source-scoped)."
curl -sf -X POST http://127.0.0.1:8080/api/v1/_reset >/dev/null   # start from the seeded, discovery-only inventory
echo
printf "${BOLD}Run 2a — sync current state = state_v1 (populates Oasis from Terraform)${RESET}\n"
uv run oasis-sync --new samples/state_v1.json
echo
printf "${BOLD}Run 2b — sync current state = state_v2 (report-generator now absent -> DESTROY)${RESET}\n"
uv run oasis-sync --new samples/state_v2.json

section "SCENARIO 3  —  Discovery scan (the third source of truth)"
echo "Terraform destroying a role is a CLAIM of deletion, not proof. The sync left"
echo "report-generator at 'decommission_pending'. Only a scan of the real cloud can"
echo "settle it — and the two possible answers are very different."
REPORT_ARN="arn:aws:iam::123456789012:role/report-generator"

echo
printf "${BOLD}Scan 1 — the role is somehow STILL THERE (failed destroy / state rm)${RESET}\n"
curl -sf -X POST http://127.0.0.1:8080/api/v1/discovery:scan \
  -H 'content-type: application/json' \
  -d "{\"live_identities\": [\"$REPORT_ARN\"]}" \
  | uv run python -c "import json,sys; [print(f\"  {t['name']}: {t['from']} -> {t['to']}\n    {t['finding']}\") for t in json.load(sys.stdin)['transitions']]"

echo
printf "${BOLD}Scan 2 — a later scan no longer sees it: deletion confirmed${RESET}\n"
curl -sf -X POST http://127.0.0.1:8080/api/v1/discovery:scan \
  -H 'content-type: application/json' -d '{"live_identities": []}' \
  | uv run python -c "import json,sys; [print(f\"  {t['name']}: {t['from']} -> {t['to']}\n    {t['finding']}\") for t in json.load(sys.stdin)['transitions']]"

section "SCENARIO 4  —  Policy gate on a clean plan  (opt-in / preventive)"
echo "A well-formed plan passes the gate; terraform apply would proceed."
uv run oasis-gate --plan samples/plan_approved.json && echo "gate exit code: 0 (apply allowed)"

section "SCENARIO 5  —  Policy gate on a risky plan  (opt-in / preventive)"
echo "Admin-on-new-role + long-lived key + wildcard trust added on update -> DENY. apply is blocked (exit 1)."
if uv run oasis-gate --plan samples/plan_denied.json; then
  echo "gate exit code: 0"
else
  echo "gate exit code: $?  (non-zero -> terraform apply is blocked)"
fi

echo
hr
echo "Demo complete. Mock Oasis will shut down now."
