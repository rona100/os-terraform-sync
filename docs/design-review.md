# Design review (self-critique)

A reviewer-hat pass over the bridge, framed at the **design level**: what the solution
can and cannot *structurally* see, where its consistency model is thin, and which use
cases and edge cases it misses. Concrete code references appear only as *evidence* of a
design gap — the point is the design, not the line. For the intentional PoC gaps see
[README · Known limitations](../README.md#known-limitations); this doc goes deeper and
is more critical.

One thing the design gets right up front: the gate **fails closed** on an Oasis outage
([gate_cli.py:60-65](../oasis_bridge/gate_cli.py#L60-L65)) — an unreachable policy
service blocks apply rather than silently allowing it. Good default. The rest of this
doc is where it falls short.

> **✅ addressed** = fixed since the review; **◐ partly addressed** = the model improved
> but the underlying risk remains. Findings are kept rather than deleted, so the
> reasoning that motivated each change stays on record. Everything unmarked is still
> fully open.

---

## 1. Coverage boundary — the design can never be authoritative alone

**Decision.** The two *Terraform-derived* hooks (gate, sync) derive everything from
Terraform's own output (plan / state). Discovery sits deliberately outside this
boundary — that is precisely why it is part of the solution rather than a footnote.

**What it structurally misses.** Anything that doesn't pass through Terraform is
invisible: console/CLI/SDK changes, other IaC tools, other pipelines, and — for the
gate — any team that simply doesn't adopt it (it's opt-in). This isn't a bug to fix;
it's the shape of the input.

**Edge case that turns into a *wrong* answer, not just a miss.** ◐ *Partly addressed.*
`terraform state rm` removes a resource from state without deleting it in AWS. The
differ used to read that as gone and emit **DESTROY / decommissioned** for a principal
that was still live — actively mislabelling lifecycle, not merely under-reporting.
Now a DESTROY only reaches `decommission_pending`, and a **discovery scan** settles it:
absent → `decommissioned`, still present → **`orphaned`**. The mislabel became an
explicit finding. **Residual:** confirmation is only as fresh as the scan cadence, so
there is still a window in which the inventory is wrong — just wrong in the safe
direction (pending, not retired).

**Design consequence.** The bridge can only ever be *one* of two imperfect sources.
Oasis discovery (which observes cloud truth) is a **first-class component of the
solution**, not a backstop bolted on afterwards: the architecture treats "Terraform
says" and "discovery says" as claims to be merged (§3), each authoritative for its own
half, with disagreement surfaced rather than resolved by precedence.

## 2. Consistency & ordering — the event-driven model is under-specified

**Decision.** Sync is triggered by the state-backend write event (S3/GCS/TFC), one
event per apply.

**What it misses.**
- **Ordering.** Object-write notifications are not ordered and are at-least-once. A
  late-arriving *older* state processed after a newer one will regress the inventory —
  resurrect a decommissioned identity, revert an owner. Terraform state carries a
  monotonic `serial`; the design ignores it, so there's nothing to reject stale events.
- **Idempotency.** Duplicate delivery reprocesses; there's no dedup key.
- **Concurrency — now demonstrated, not hypothetical.** Inventory mode reads the whole
  inventory, computes a diff, then writes: a TOCTOU window with no optimistic
  concurrency (etag/version). Adding discovery made this concrete — there are now **two
  independent writers** to the same records, both unlocked FastAPI handlers:
  `mock_oasis/discovery.py` mutates `lifecycle_status`, and `mock_oasis/server.py`
  writes via `_merge`. So a scan can flip a record to `decommissioned` while a sync is
  mid-merge, or a sync can resurrect a record a scan just confirmed gone. Ironically the
  fix for §1 (a second source of truth) sharpened this one: more sources means more
  writers, and nothing serialises them.

**Design direction.** Gate on state `serial` for monotonicity + idempotency; use
conditional writes (version/etag) so a reconcile fails safely instead of clobbering.

## 3. Reconciliation semantics — replace, not merge  ✅ *addressed*

**Decision (original).** On sync the server did `INVENTORY[ident_id] = identity` — a
wholesale replace with Terraform's view.

**What it missed.** Different fields have different rightful owners: Terraform owns
*provenance* (owner, source, classification) while discovery owns *runtime* (`last_used`,
`is_stale`, rotation timestamps). A blind replace discarded the runtime half — the
enrichment story silently caused data loss on exactly the fields discovery is good at.

**Now fixed.** The translator leaves discovery-owned fields `None` instead of guessing,
and the server merges via `_merge` / `DISCOVERY_OWNED_FIELDS` — an incoming `None` for a
discovery-owned field is dropped, never written. This isn't cosmetic: preserving
`last_used`/`is_stale` is what makes the `deprecated` lifecycle state derivable at all.
See [architecture.md · field ownership](architecture.md#field-ownership--merge-semantics).
**Still open:** per-field ownership is a hard-coded tuple, not a declared schema
contract, and there's still no optimistic concurrency on the write (§2).

**Join-key assumption.** Reconciliation assumes discovery keys records by the same ARN
Terraform uses. If discovery keys by a different scheme (account+name, a provider
resource id), Terraform records won't match and the "land on top of the discovered
record" behaviour **silently becomes duplication** instead of enrichment. This is the
single assumption the whole value prop rests on, and it's untested.

**Design direction.** Model the inventory as the reconciliation authority with explicit
**per-field source-of-truth** and a merge (not replace); define the identity join key as
a first-class contract, with a fallback matcher when ARNs don't line up.

## 4. Privilege model — too narrow to be a real guardrail

**Decision.** Privilege is inferred from the `managed_policy_arns` argument on identity
resources, and the gate's admin rule fires on resource creation.

**What it misses (each a real escalation path the gate would approve):**
- **Updates.** `rule_no_admin_on_new_nhi` only triggers on `action == "create"`
  ([policy.py:33](../mock_oasis/policy.py#L33)). Attaching `AdministratorAccess` to an
  **existing** role is an `update` and passes.
- **Attachment / inline resources.** Most Terraform grants policies via separate
  resources (`aws_iam_role_policy_attachment`, `aws_iam_policy_attachment`) or inline
  (`aws_iam_role_policy`, `inline_policy`). None are in `IDENTITY_RESOURCE_TYPES`
  ([models.py:16](../oasis_bridge/models.py#L16)), so the **common** way to grant admin
  is invisible to both the gate and the differ's privilege note.
- **Trust policy.** ◐ *Partly addressed.* The **translator** now parses
  `assume_role_policy` to derive `credential_type` (`federated` / `service` /
  `static_key` / …), so *who may assume the role* is no longer invisible to the model.
  But the **policy rules never see it** — `mock_oasis/policy.py` inspects only
  `after.managed_policy_arns` and `tags`. So a change admitting a new
  external/cross-account principal (wildcard `Principal`, missing `ExternalId`,
  confused-deputy) still sails through the gate. The exposure is unchanged; only our
  *visibility* into it improved.
- **Boundaries & non-identity grants.** Permission boundaries are ignored (false
  positives and negatives); privilege granted by non-identity resources (a standalone
  `aws_iam_policy` with `Action:"*"`, S3/KMS/SQS resource policies) is out of scope.

**Design direction.** Reason about **effective permissions** for an identity — walk the
attachment/inline/trust-policy graph and evaluate the *union*, on create **and** update
— rather than reading a single inline argument on one resource type.

## 5. Identity & lifecycle modeling — address vs. ARN mismatch

**Decision.** Lifecycle is diffed by TF **address**; identity in Oasis is keyed by
**ARN**. These are different keys, and the seam shows:

- **Rename / `moved`.** Renaming a resource (a `moved {}` block or `state mv`) changes
  the address but not the ARN → the differ emits DESTROY(old address) + CREATE(new
  address) for the *same* identity. A false decommission and re-register.
- **Replace.** A `replace` keeps the address but mints a new ARN. Two-file
  `diff_states` emits an UPDATE carrying the **new** ARN and never decommissions the
  **old** one — the old principal's record lingers `active` forever. (Notably, inventory
  mode *does* catch this: the old ARN is absent from the desired set, so it goes to
  `decommission_pending` and the next scan confirms it gone — the resolution is now
  end-to-end. A correctness **asymmetry** between the two sync modes worth being
  explicit about.)

**Narrow NHI surface.** ◐ *Partly addressed:* the model now covers OIDC/SAML federation
anchors, `service_linked_role`, `google_service_account`, and Secrets Manager, and
derives `credential_type` (`federated` / `service` / `static_key` / …) from the trust
policy — so workload identity federation is represented (with the guardrail caveat in
§4). Still missing: Azure, STS sessions, groups, and any notion of an identity's
effective permissions across accounts.

**Design direction.** Treat **ARN as the identity key** with address as metadata, so
moved/replace are handled by identity continuity rather than address bookkeeping.

## 6. Multi-tenancy, workspace & scale

- **Workspace scoping** (already documented) is, at scale, a *data-corruption* risk, not
  just a caveat: one inventory fed by many states means "absent from this state" can't be
  distinguished from "owned by another workspace" without a workspace/state key on each
  record.
- **Scale.** Inventory mode pulls the **entire** inventory (no pagination/filter) and
  diffs it in memory — fine for a demo, not for a real estate of identities.

## 7. Integration & trust model

- **Opt-in + TOCTOU.** The gate only protects pipelines that adopt it, and only if
  `apply` consumes the exact **gated** plan (`-out`); a regenerated plan or
  `-auto-approve` elsewhere bypasses it.
- **AuthN/Z.** The client ships plan/state (which can carry sensitive values) to Oasis
  with no authentication token or mTLS. Fine for a mock; a real gap.
- The CI-step-vs-provider choice itself is discussed in
  [architecture.md](architecture.md#design-decision-ci-step-vs-terraform-provider).

---

## Design directions (summary)

1. ✅ **Two imperfect sources, reconciled** *(field level)* — *done*: writes merge with
   per-field source-of-truth, so Terraform never blindly overwrites discovery. Residual:
   the ownership split is a hard-coded tuple rather than a declared schema contract, and
   the write still has no optimistic concurrency (§2, §3).
2. ◐ **Confirm before retiring** *(state level)* — *partial*: no terminal lifecycle
   state on a single source's claim. Terraform destroying an identity is *intent*, so it
   lands at `decommission_pending`; a scan supplies the *proof*, and when the two
   disagree the conflict surfaces as `orphaned` instead of a silent mislabel. Residual:
   correctness is bounded by scan cadence — the record is wrong until the next scan, but
   wrong in the safe direction (§1).
3. **Order & isolate** — gate on state `serial`; conditional/versioned writes. Now more
   urgent, not less: a second writer (discovery) makes the unserialised-write problem
   real (§2).
4. ◐ **Effective permissions** — *partial*: the trust policy is now parsed for
   `credential_type`, but nothing evaluates the attachment + inline + trust-policy graph
   as a union, and the policy rules still read one inline argument on create only.
5. **ARN-keyed identity** — address is metadata; handle moved/replace via identity
   continuity.
6. **Scope by workspace** — stamp records with their originating state key.
7. ◐ **Broaden the identity surface** — *partial*: OIDC/SAML federation anchors,
   `service_linked_role`, `google_service_account` and Secrets Manager are modelled;
   Azure, STS sessions, groups and cross-account effective permissions are not.

None of these change the thesis (Terraform provenance + Oasis discovery are better
together); they harden the *reconciliation* between two sources that are each, alone,
incomplete.
