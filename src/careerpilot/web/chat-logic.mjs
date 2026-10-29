import {showText, reviewState} from './logic.mjs';
export const prompts = ['Show my jobs', 'Analyze job', 'What information is missing?',
 'Prepare my application', 'Show my applications', 'Show my pending approvals'];
export function direction() { return 'ltr'; }
export function canSelect(card, conversation) {
 return card.kind === 'selection' && !!conversation.pending && !!conversation.awaiting;
}
export function outcomeLabel(thread) {
 const status=thread.review.status;
 const labels={pending:'Pending human approval',approved:'Approved; execution unproven',
 applied:'Local record updated',simulated:'Simulated only — no external delivery',
 failed:'Definite failure',unknown:'Unknown outcome — do not retry sending',invalidated:'Invalidated proposal',
 executing:'Execution claimed — outcome not yet confirmed',rejected:'Rejected',revision_requested:'Revision requested'};
 return labels[status] || status;
}
export function textBlock(document, tag, text) {
 const el=showText(document,tag,text);el.dir='ltr';return el;
}
export function approvalEnabled(thread) { return reviewState(thread).canDecide; }

export function emptyResults(card) {
 if(['jobs','selection'].includes(card.kind))return !(card.choices?.length);
 if(['applications','approvals'].includes(card.kind))return !(card.payload?.items?.length);
 return false;
}

export function modeLabel(runtime){
 if(!runtime)return 'Connect to check assistant mode';
 if(runtime.mode==='llm')return runtime.ready?'LLM assistant · External actions simulated':'LLM setup required · No demo fallback';
 return 'Deterministic demo · No LLM · Simulated sending';
}
export function messageLabel(message){return message.role==='user'?'You':message.mode==='llm'?'CareerPilot · LLM':'CareerPilot · demo';}
