# Review policy

Read [AGENTS.md](../AGENTS.md) and the repository engineering conventions first.
This staged R0/R1/R2 policy preserves current native protection, workflows,
approval gates and required contexts. Neither this document nor the advisory
provider configuration supplies an enforcement waiver.

## Scope

Classify each complete change by its actual behavior and affected consumers.
R0/R1 eligibility does not exempt protected operations or R2 work. The policy PR
itself is R2. This change adds no execution, deployment, account or billing
permission, selector, workflow exemption or automatic approval.

## Required checks

Current required union of classic protection and active rulesets (preserved):

- `Repository hygiene`
- `gitleaks`
- `pr-template`
- `risk-gate`
- `test-summary`

Preserve the union of classic protection and active default-branch rulesets,
including strictness, app bindings, review, conversation, administrator, push,
deletion and bypass controls. Job exports are evidence outside the repository.
No native setting, required-check list or workflow changes in this PR.

A future enforcement migration requires separate authorization and verification.
Only after substantive PR-template evidence and authenticated approval verification
are incorporated into authoritative risk-gate enforcement may pr-template become
advisory. It remains required wherever currently enforced. CodeRabbit is
supplementary; any currently enforced provider dependency remains authoritative.
Preserve duplicate scanners until equivalent coverage is separately demonstrated.
Re-read context names, app bindings and settings before any future migration.

## Risk tiers and acceptance

Classify the complete diff, including renames, deletions and affected consumers,
against trusted default-branch policy. The author cannot lower a tier with a label
or PR-side configuration. Unknown scope is R2/pending. File extensions alone do
not determine risk; these policy changes themselves are R2.

| Tier | Scope | Required evidence |
| --- | --- | --- |
| R0 | Prose without executable examples or contract changes; bounded skills without authority changes; additive isolated tests without weakened assertions or harness changes | Complete exact diff, shared redaction/secret scan, relevant hygiene/docs/manifest checks, actual execution of added tests, and a small bot-owned exact-head acceptance receipt |
| R1 | Ordinary implementation, refactoring and non-sensitive dependencies/contracts | Relevant unit/integration/type/lint/build checks, affected consumer contracts, secret scan, regression/adverse evidence for changed behavior, and one independent Codex review commissioned by the dedicated bot |
| R2 | Risk/orders/live paths, reconciliation financial semantics, auth/secrets/masking, deploy/IAM, launcher guards, CI/approval policy, CODEOWNERS, authority-changing skills, or tests weakened/deleted in ways that remove safety guarantees | Sol high review, full relevant suites, meaningful adverse cases for applicable redaction/fail-closed/concurrency/stale-state risks, and authenticated current-head owner risk/policy approval |

Required executable checks remain required. Runtime suites may be N/A for R0 only
when trusted complete-diff selection proves them inapplicable; no selector or
exemption is introduced here. Missing, error, transport failure and unknown are
never N/A or PASS. A required test skipped because the PR is Draft is not proof
that it passed. Proven inapplicable Dependabot/release jobs and provider skips are
recorded separately. Known base failures do not silently waive required red checks.
Substantive unresolved findings and valid REQUEST_CHANGES reviews still block;
cosmetic suggestions do not create mandatory repair work.

## Review authority and receipt

The dedicated code-review-and-merge bot is the sole PR review/merge authority.
No ad-hoc second PR reviewer is dispatched. Workers publish Drafts only and never
merge, undraft or label them. R1 independent review and R2 Sol high review are
commissioned within that bot's established process. R0 uses its bounded acceptance
receipt. Reuse a receipt only for the same complete reviewed candidate.

Bind acceptance to repository, PR, full head, base/policy version, tier, authorized
scope, issuer and any expiry/revocation. Head/base/policy drift invalidates the
receipt. Re-read heads and checks before any separately authorized merge. An
`owner-approved` label is a display hint, not an authenticated exact-head receipt.
If original owner evidence cannot be authenticated, retain `owner_needed`.
R0/R1 routine technical acceptance needs no additional owner card within existing
authorization; R2 needs the applicable owner decision. Source approval never grants
deployment, activation, accounts/amounts, seeds, spending or secret access.

CodeRabbit is supplementary. Its quota, availability or green icon supplies neither
primary review nor owner permission. Manual on-demand reviews may include Drafts;
automatic requests, automatic incremental reviews, automatic labels and provider
auto-approval are disabled in the proposed configuration. No `review-ready` label
is applied by this change. Do not widen `max_merge_risk` beyond `low`.

## Model routing

Preserve explicit host model/effort choices and actual availability. Sol medium
remains the default. Record assigned and resolved model; unavailable models or
unsupported efforts stop the affected launch rather than silently falling back.
Model tiers never change authorization or required verification.

| Work | Model / effort | Escalation |
| --- | --- | --- |
| Known-schema readbacks, report formatting, bounded inventory, prose | `gpt-6.1-terra` low | Medium when sources disagree |
| R0 isolated additive tests or small documented patch | Terra medium | Sol medium if behavior, harness or consumer effects emerge |
| Routine backend/API work, ordinary refactoring, R1 review | `gpt-6.1-sol` medium | High for subtle state/concurrency or a failed repair with a concrete unresolved cause |
| Risk, expiry/revocation, transaction safety, masking/secrets, launcher or approval-policy change; R2 review | Sol high | Keep domain adverse tests and owner prerequisites |
| Cross-module architecture or ambiguous subsystem trade-offs | `gpt-6-astra` medium | High only for a named unresolved architecture question |
| Small R0 acceptance, commissioned only by the established bot | Terra low/medium | Sol medium if uncertain; Sol high for protected behavior or authority |

Claude designs/plans/reviews frontend work; Qwen through Qoder implements it.
Codex handles backend/ops/offline work. Cursor stays Auto (`default`), never Fast
or a named model. This policy does not modify launcher arguments, host settings,
repository exclusivity or queue admission, and launches no model trial or paid call.
