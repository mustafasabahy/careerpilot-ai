# ADR-006: Evidence-linked, constrained career preparation

Status: accepted for Phase 6 local development.

## Problem

Existing assessments do not retain full normalization policy; document versions
have no job/assessment link. Free prose generation could introduce achievements
that the profile never supplied. An original CV may contain unverified assertions.

## Decision

Add an immutable PreparationRecord snapshot linking assessment, job, candidate,
fact provenance and two versioned documents. Reproduce existing assessments before
explaining them. Add an atomic, additive SQLite v1-to-v2 migration without rewriting
prior aggregates. Keep deterministic evidence comparison and rendering outside
LangGraph and provider code. Introduce an exchangeable fact-selection planner,
mock by default, whose only output is whitelisted profile fact IDs. Render exact
confirmed facts and controlled templates; preserve raw originals and return diffs.
Require explicit server-side draft and real-model export permissions.

## Consequences

Claims and document lineage are reviewable; unsupported model output cannot enter
prose. Existing modules/workflows remain available. No new package is needed.
Drafts have limited stylistic quality; profile fact editing, rich formatting and
external verification are deferred. Source-document evidence alone cannot enter
a draft until confirmed. Rule context must accompany older assessments. SQLite v2
cannot be opened by older application versions. Human review remains mandatory;
these artifacts do not submit applications or predict hiring outcomes.
