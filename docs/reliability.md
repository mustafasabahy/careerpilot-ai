# Reliability and recovery — Phase 9

All six graph runners apply shared cooperative execution controls. Each active
invocation has a UUID operation ID, correlation ID and workflow ID (derived from
request/thread IDs where available). Nested workflows share one budget; streaming
binds context only while advancing the iterator. Optional owner-scoped
SQLiteRunJournal records invocation counters/outcomes and cancellation requests in
a separate file. Reusing an invocation ID is rejected. Recovery uses a new invocation
ID linked to the same workflow; authoritative action IDs/attempts prevent duplicate
effects. An abandoned journal entry remains running, not falsely successful.

ExecutionLimits defaults: 32 node executions, 30 adapter/tool calls, 30 retrieved
records including duplicates/reloads, 3 model attempts, 120 active seconds. Configure
Python RunControl/ExecutionLimits or CAREERPILOT_RUN_MAX_STEPS, MAX_TOOL_CALLS,
MAX_RETRIEVED_JOBS, MAX_MODEL_CALLS, OVERALL_SECONDS in CLI composition. Human waiting
time is governed by proposal expiry; resume gets a new active-invocation budget.
Limits are checked between operations; synchronous Python cannot be forcibly killed
safely. Network/model timeouts receive remaining active time. SQLite waits are
bounded and outcome/rollback bookkeeping retains a small cleanup window. No live
job-board or sending adapters exist; adapters must honor their timeout contract.

Provider max_retries remains 0 by default and can be explicitly configured 0–2.
SDK retries are always disabled so the application counts every attempt. Only
transient model timeouts/connections/408/500/502/503/504 and genuine rate limits can
retry. Quota/authentication/input/refusal/output/unknown failures do not. Retry-After
seconds, HTTP dates and retry-after-ms are parsed; a delay exceeding the backoff
cap or remaining time stops retries rather than shortening provider guidance.
Discovery read_retries is explicit, default 0; generic source failures are not
assumed transient. There are no automatic write/approval/submission retries.

RunControl.cancel or an owner-scoped journal request stops an invocation at its next
boundary. ActionService.cancel separately persists action cancellation: pending
becomes rejected, approved becomes invalidated/cancelled, executing becomes unknown.
Stopping one invocation does not revoke a durable approval; use action cancellation
for that purpose. Nothing can unsend an already-started attempt. The additive
cancelled outcome reason is understood by current code; older readers may reject
that payload, although business schema stays v3.

## Failure-mode table

| Failure | Detection/report | Recovery | Remaining limit |
| --- | --- | --- | --- |
| Invalid input/output, auth, configuration | Safe permanent taxonomy code | Correct input/grant/config; fresh call | No retries |
| Model read timeout/transient HTTP | Explicit retryable error | At most configured retries, counted budget/backoff | Requests may incur cost even without response |
| Rate limit/quota | Header/code classification | Respect guidance or stop; quota is permanent | No provider service tested live |
| Overall time/resource cap | Nonretryable ExecutionFailure | Inspect correlation IDs, deliberate fresh invocation | Cooperative, not hard preemption |
| SQLite busy/integrity/write failure | Numeric error code and rollback | No automatic transaction replay; inspect durable records | No HA/power-loss guarantee |
| Interrupted checkpointed workflow | Persistent SqliteSaver state | Same authorized thread, existing decision | Earlier noncheckpointed graphs retain prior behavior |
| Effect acknowledged but outcome write fails | Durable claim exists, no outcome | Seal unknown on recovery; never resend | Reconcile independently |
| Lost external acknowledgement | unknown/uncertain outcome | Inspect simulator receipt ledger; no blind retry | No live reconciliation adapter |
| Cancellation before/after claim | Token or persisted action state | Reject/invalidate before claim; unknown after claim | Cannot retract in-flight effect |

SafeJsonFormatter emits only allowlisted event/error codes and UUID correlation
fields, never source text, credentials, exceptions or arbitrary messages. Existing
business errors/results remain compatible; failure_report maps them to one safe
report. A typed unknown action outcome marks the invocation uncertain/failed.

Tests exercise budgets, permanent/transient failures, retry guidance, isolation,
rollback and a separate-process crash after the simulator committed its effect but
before the business outcome. Recovery produces unknown with one effect and one
approval/claim; completed review steps are not repeated. See official
[interrupt replay](https://docs.langchain.com/oss/python/langgraph/interrupts) and
[graph limits](https://docs.langchain.com/oss/python/langgraph/graph-api) guidance.
