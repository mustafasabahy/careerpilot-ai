import {showText, reviewState} from './logic.mjs';
export const prompts = [
 ['Show my jobs', 'اعرض الوظائف'], ['Analyze job', 'حلل الوظيفة'],
 ['What information is missing?', 'ما المعلومات الناقصة؟'],
 ['Prepare my application', 'جهز التقديم'], ['Show my applications', 'اعرض طلباتي'],
 ['Show my pending approvals', 'اعرض الموافقات المعلقة'], ['Select job', 'اختار وظيفة']
];
export function direction(text) { return /[\u0600-\u06ff]/.test(text) ? 'rtl' : 'ltr'; }
export function canSelect(card, conversation) {
 return card.kind === 'selection' && !!conversation.pending && !!conversation.awaiting;
}
export function outcomeLabel(thread) {
 const status=thread.review.status;
 const labels={pending:'Pending human approval / في انتظار الموافقة',approved:'Approved; execution unproven',
 applied:'Local record updated / تم تعديل السجل المحلي',simulated:'Simulated only — no external delivery',
 failed:'Definite failure',unknown:'Unknown outcome — do not retry sending',invalidated:'Invalidated proposal',
 executing:'Execution claimed — outcome not yet confirmed',rejected:'Rejected',revision_requested:'Revision requested'};
 return labels[status] || status;
}
export function textBlock(document, tag, text) {
 const el=showText(document,tag,text);el.dir='auto';return el;
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
export function messageLabel(message){return message.role==='user'?'You / أنت':message.mode==='llm'?'CareerPilot · LLM':'CareerPilot · demo';}
