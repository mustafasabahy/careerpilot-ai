# Manager demo — 5–10 minutes, synthetic only

## Preparation

```sh
uv sync --locked
uv run --locked careerpilot-server --directory work/manager-ui --init
uv run --locked careerpilot-server --directory work/manager-ui
```

Open http://127.0.0.1:8000, read the operator's private local dev-access.json file,
paste its token, Connect, then Load synthetic examples. Do not show the token in
screenshots or a shared terminal. All candidate/job facts are synthetic. The package
fixtures provide a candidate and multiple postings with supported/unknown/gap cases.
No provider account/key is needed.

## Demonstration

1. **0–2 minutes — analysis.** Select the candidate and first Python job. Analyze.
   Show mandatory-rule decision, supported matches, unknowns and source evidence.
   Explain that this is not a hiring prediction; change to the mixed-requirement
   job to show gaps/unsupported requirements when useful.
2. **2–4 — honest drafts.** Prepare evidence-linked drafts. Review CV/cover text,
   version metadata/change diff, supporting facts and missing-data questions. Show
   interview questions and timed practice sessions. Unverified SQL/achievements are
   not silently promoted into confirmed prose.
3. **4–6 — human approval.** Track the selected job using the CV. Stage a ready-status
   change. Show waiting state and exact proposal/version/hash, then explicitly approve.
   The local application record changes only after confirmation. Stage a recruiter
   message to show approve/reject/revise; sending is simulated, not actual delivery.
4. **6–8 — recovery/failure.** Refresh/reconnect to the saved workflow and inspect
   audit history. Repeat recovery: no second effect. Show the scripted lost-
   acknowledgement example below: it stays unknown, never retries submission.
5. **8–10 — evaluation.** Run the offline dataset, inspect baseline/results and safe
   local metadata/counters. Explain that real LangSmith export is optional and not
   needed for independent local logs. No live telemetry/model quality is claimed.

For a complete reproducible terminal demonstration, use a fresh directory each run:

```sh
uv run --locked python scripts/manager_demo.py --directory work/demo-run-001 --output work/demo-run-001.json
uv run --locked careerpilot-evaluate --output work/eval-manager.json --baseline evals/baseline-v1.json
uv run --locked pytest tests/test_restart_recovery.py tests/test_reliability.py -q
```

The script executes real analysis/draft persistence, pauses, closes/reopens resources,
approves/resumes, recovers repeatedly, and checks a single effect. A second distinct
approved action loses its acknowledgement, seals unknown and does not duplicate.
It inspects the independent simulator receipt and runs a bounded transient retry
(two counted attempts), then all dataset cases. Output is actual synthetic data.
Use this file for evidence inspection if a live UI demonstration is inconvenient.

## Technical decisions and trade-offs

- Modular monolith: pure rules/application ports; LangGraph only coordinates nodes.
- Pydantic + explicit evidence states: missing/unknown/unverified are not invented facts.
- Reference-only checkpoints + separate confirmed memory: easier correction/privacy.
- Immutable exact-version approval + unique claim: conservative unknown beats duplicate sending.
- SQLite single host: small reproducible environment, no implied HA/production scalability.
- Offline template drafts and manual metadata tracing: truth/privacy over stylistic freedom.
- FastAPI/static JS: minimal usable transport without moving business rules into UI.

Quality command: `sh scripts/check.sh` (Python gates, actual tests, UI logic tests and
strict deterministic baseline evaluation). Packaging: `uv build`. Deployment,
backup/restore, deletion and launch prerequisites are in deployment.md, operations.md
and known-limitations.md. No externally consequential action is part of this demo.

## Chat-first walkthrough

For existing data, stop service, back up and run --migrate before restarting. Token
handling is unchanged: after refresh enter the same owner token and Connect.

1. Open the chat shell and load synthetic examples only in an empty demo account.
2. Send “Show my jobs”, then “Analyze job”. Choose the mixed job by its ID suffix.
3. Refresh the browser while awaiting profile selection. Reconnect with the same token:
   the saved conversation still asks for the profile. Choose Synthetic Candidate.
4. Inspect met/unknown/gap findings and source evidence. Send “What information is missing?”.
5. Send “Prepare my application”. Review fact-linked CV, cover letter, document versions and practice
   plan. Unverified SQL must not become a confirmed skill in the generated draft.
6. Open Workspace details to create/track a synthetic application and stage an action
   using the retained controls. Return to chat; send “Show my pending approvals”.
7. Send “yes”: it returns help and does not approve. Use the explicit approval/reject
   control on the proposal card. Local changes say applied; simulated sending is labelled
   simulated and never claimed as an external submission.
8. Reconnect/reload and inspect the same workflow state/audit. For uncertain external
   outcomes use the existing scripted failure scenario; it never blindly resends.
9. Create another conversation: job/profile selection is independent. Delete that
   transcript only; business/audit records remain. Run sh scripts/check.sh for regression.
