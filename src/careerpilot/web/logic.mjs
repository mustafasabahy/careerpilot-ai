export function reviewState(thread) {
  if (!thread) return {label: 'No staged action', canDecide: false};
  const status = thread.review.status;
  return {label: thread.paused && status === 'pending' ? 'Waiting for your approval' : status,
    canDecide: thread.paused && status === 'pending'};
}
export function showText(document, tag, text) {
  const element = document.createElement(tag);
  element.textContent = String(text);
  return element;
}
export function decisionPayload(proposal, decision, reason) {
  return {id: crypto.randomUUID(), proposal_id: proposal.id, thread_id: proposal.thread_id,
    proposal_version: proposal.version, content_sha256: proposal.content_sha256, decision,
    reason: reason || null};
}
export function evidenceForFinding(finding, profile, requirement) {
  const references = new Set(finding.evidence_ids || []);
  const found = new Map();
  function visit(value) {
    if (!value || typeof value !== 'object') return;
    if (references.has(value.id) && typeof value.origin === 'string' && typeof value.reference === 'string') {
      found.set(value.id, {id:value.id, origin:value.origin, reference:value.reference});
    }
    for (const child of Object.values(value)) visit(child);
  }
  visit(profile); visit(requirement);
  return [...references].map(id => found.get(id) || {id, status:'Source reference not loaded'});
}
