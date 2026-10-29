# Known limitations and launch blockers

This is a verified local technical preview, not a certified production service.

## Before real personal data or public launch

1. Review identity/SSO/MFA, token distribution, abuse/rate limiting, trusted proxy/TLS,
   workload limits and incident ownership. Current authentication is local operator-
   issued short-lived opaque bearer access, not an enterprise account system.
2. Decide audit-retention and whole-account erasure policy. Sensitive action proposals
   block destructive profile deletion; backups/pages are not forensically erased.
   Memory deletion is separate from profile/documents/audit deletion. No GDPR claim.
3. Validate Linux supervisor/TLS and paired backup/restore on target hardware. SQLite
   is one-host/local-volume/single-worker; no HA, PostgreSQL, power-loss, network-FS,
   RTO/RPO, distributed queue or production load guarantee has been established.
4. Do not add a real sender without provider idempotency, timeouts, destination binding,
   receipt/reconciliation and explicit consent review. Current external effects are
   simulated and never count as an actually submitted application.
5. Live LLM/LangSmith behavior, costs and real extraction quality remain unverified.
   Offline evaluation is a 12-case synthetic contract regression suite, not a broad
   benchmark or an LLM judge's ground truth. Cost is null unless actually measured.

## Scoped product/architecture limits

- Evidence comes from user/source declarations, not independent verification. Baseline
  rules support selected skill/language/authorization cases; unsupported requirements
  stay unknown. No hiring probability, discrimination assessment or legal eligibility
  opinion is generated.
- CV/cover prose is conservative plain text from confirmed source facts. No rich
  DOCX/PDF export, fabricated metrics, semantic rewriting or professional design service.
- Profiles are immutable snapshots; revisions do not retarget existing applications.
  There is no complete profile-edit migration/account merge flow.
- API jobs are registered JSON/local postings, not external searching. Query filters
  one owned page; the current UI shows the first page. Global source/URL uniqueness
  in the historical business schema can cause cross-account import conflicts on the
  same URL. A tenant-aware uniqueness migration requires deliberate design review.
- Context/approval graphs are checkpointed; earlier analysis/extraction/discovery/
  preparation graphs retain their own idempotent business persistence, not generic
  crash checkpoints. Human workflows span requests; no background live-model queue
  or streaming protocol is claimed.
- Budgets/deadlines are cooperative per active invocation, not forced cancellation
  of arbitrary Python or a global tenant billing cap. Each explicit resume gets a
  new active budget; proposal expiry still limits approval validity.
- Cross-file security ownership and business storage are fail-closed, not atomic
  distributed transactions. Crashes can leave inaccessible reservations/orphans.
  Listing skips absent records; administrative cleanup/paired restore may be needed.
- Unknown action outcomes are deliberately sealed and never blindly retried. A crash
  before dispatch or a concurrent observer can conservatively mark unknown even
  when no effect occurred. Simulator receipts can be inspected independently; no
  automated live-provider reconciliation or cancellation-after-dispatch exists.
- Browser initial layout/controls were visually inspected and UI logic tested with
  Node. Full authenticated browser E2E, assistive-technology and mobile coverage are
  not claimed; backend integration tests cover the full actual API flow.
- GitHub CI is supplied but no remote repository/CI run exists. Pin reviewed action
  SHAs when adopting it. Current pip-audit found no known advisories among scanned
  distributions, which is not proof of absence of vulnerabilities.

## Chat-first interface

The chat assistant matches documented English commands only; it does not
understand arbitrary paraphrases, edit documents, upload files or perform external
search/sending. Domain explanations/drafts are English; user/provider content may
conversation. Tokens must be re-entered after refresh. History and pending selections
persist, but automatic authentication restoration is deliberately absent.

Transcript/business writes cross database files: durable operation IDs and explicit
safe replay cover existing deterministic workflows, not distributed exactly-once
execution. SQLite serializes transcript writes across conversations. Deleted history
may remain in backups until retention expires; business/approval audits are separate.
Node UI tests and manual browser checks are not a maintained browser E2E regression
suite or accessibility/mobile-device certification. See chat.md.
