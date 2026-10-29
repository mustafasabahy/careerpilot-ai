import {api} from './app.js';
import {decisionPayload,evidenceForFinding} from './logic.mjs';
import {prompts,direction,canSelect,outcomeLabel,textBlock,approvalEnabled,emptyResults,modeLabel,messageLabel} from './chat-logic.mjs';
const $=id=>document.getElementById(id);
const text=(tag,value)=>textBlock(document,tag,value);
let conversation=null, messages=[], operations=[], conversationsOffset=0, nextMessages=null;
let busy=false, retryRequest=null, runtime=null;
$('chat-access').append($('workspace-access'));
$('token').addEventListener('input',()=>{conversation=null;messages=[];operations=[];retryRequest=null;$('conversation-list').replaceChildren();render();status('Connect to load this account');});
function status(value,kind='completed'){$('chat-status').textContent=value;$('chat-status').dataset.state=kind;}
function button(label,action){const el=text('button',label);el.type='button';el.addEventListener('click',()=>run(action));return el;}
async function run(action){if(busy)return;busy=true;$('chat-send').disabled=true;$('chat-shell').setAttribute('aria-busy','true');
 try{await action();}catch(error){status(error.message,'error');}
 finally{busy=false;$('chat-send').disabled=false;$('chat-shell').setAttribute('aria-busy','false');}}
function detailLink(label,hash){const a=text('a',label);a.href='#'+hash;return a;}
function revealDetails(){if(location.hash && !['#chat','#workspace-details'].includes(location.hash))$('workspace-details').open=true;
 if(location.hash==='#workspace-details')$('workspace-details').open=true;}
window.addEventListener('hashchange',revealDetails);revealDetails();
async function listConversations(offset=0){const page=await api('/conversations?offset='+offset);if(!offset)$('conversation-list').replaceChildren();
 for(const item of page.items){const b=button('Conversation · '+new Date(item.created_at).toLocaleString()+' · '+item.id.slice(0,6),()=>load(item.id));
 b.dataset.conversation=item.id;$('conversation-list').append(b);}
 conversationsOffset=page.next_offset;$('more-conversations').hidden=page.next_offset===null;}
async function load(id,offset=0){const page=await api('/conversations/'+id+'?offset='+offset);conversation=page.conversation;
 if(!offset){messages=page.messages;operations=page.operations;}else{messages.push(...page.messages);operations.push(...page.operations);}
 nextMessages=page.next_offset;$('more-messages').hidden=nextMessages===null;retryRequest=null;render();
 status(conversation.pending?'Clarification saved — choose below':'Conversation restored');}
async function create(){const item=await api('/conversations','POST',{});await listConversations();await load(item.id);}
async function connect(){runtime=await api('/chat/config');$('chat-mode').textContent=modeLabel(runtime);$('chat-account').open=false;const page=await api('/conversations');await listConversations();if(page.items.length)await load(page.items[0].id);else await create();}
window.addEventListener('workspace-connected',()=>run(connect));
window.addEventListener('workspace-seeded',()=>run(()=>load(conversation.id)));
$('new-chat').addEventListener('click',()=>run(create));
$('more-conversations').addEventListener('click',()=>run(()=>listConversations(conversationsOffset)));
$('more-messages').addEventListener('click',()=>run(()=>load(conversation.id,nextMessages)));
$('delete-chat').addEventListener('click',()=>run(async()=>{if(!conversation)return;
 if(!confirm('Delete this transcript? Business records and approval audits remain.'))return;
 await api('/conversations/'+conversation.id,'DELETE');conversation=null;messages=[];operations=[];await connect();}));
async function send(value,selection=null,offset=0){if(retryRequest)throw Error('Resolve the previous request with Retry or reconnect before sending another.');if(!conversation)throw Error('Connect first');
 const request={text:value,idempotency_key:crypto.randomUUID(),selection,offset};
 await submit(request);}
async function submit(request){status('Working — awaiting confirmed server response','loading');
 retryRequest={conversationId:conversation.id,request};
 try{const op=await api('/conversations/'+conversation.id+'/messages','POST',request);
 retryRequest=null;$('chat-input').value='';await reloadAll();
 status(op.status==='completed'?'Completed':op.status,'error');
 if(op.status==='completed')status(op.task_status==='awaiting_clarification'?'Waiting for your selection':op.task_status==='awaiting_approval'?'Waiting for explicit approval':'Completed');
 }catch(error){if(error.status>=400&&error.status<500&&error.status!==429)retryRequest=null;status(error.message+(retryRequest?' — Retry reuses the same request key.':''),'error');render();throw error;}}
async function reloadAll(){const id=conversation.id;await load(id);while(nextMessages!==null)await load(id,nextMessages);}
$('chat-form').addEventListener('submit',event=>{event.preventDefault();const value=$('chat-input').value.trim();if(value)run(()=>send(value));});
$('chat-input').addEventListener('input',()=>{$('chat-input').dir=direction($('chat-input').value);});
$('chat-input').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('chat-form').requestSubmit();}});
for(const prompt of prompts){$('chat-prompts').append(button(prompt,()=>send(prompt)));}
function disclose(label,value){const d=document.createElement('details');d.append(text('summary',label),text('pre',typeof value==='string'?value:JSON.stringify(value,null,2)));return d;}
async function refreshOperation(op){await api('/conversations/'+conversation.id+'/operations/'+op.id+'/refresh','POST',{});await reloadAll();}
async function decision(thread,choice){const id=thread.review.proposal.thread_id;
 const current=await api('/threads/'+id);
 const p=current.review.proposal, displayed=thread.review.proposal;
 if(p.id!==displayed.id||p.version!==displayed.version||p.content_sha256!==displayed.content_sha256||!approvalEnabled(current))throw Error('Proposal changed or already reviewed. Refresh.');
 if(choice==='approve'&&!confirm('Approve this exact proposal version/hash? External actions are simulated.'))return;
 const value=await api('/threads/'+id+'/decision','POST',decisionPayload(displayed,choice,null));
 status(outcomeLabel(value));await reloadAll();}
function approvalCard(root,thread){root.append(text('h4',outcomeLabel(thread)),disclose('Exact proposal',thread.review));
 if(approvalEnabled(thread))for(const [label,choice] of [['Approve exact proposal','approve'],['Reject','reject'],['Request revision','revise']])root.append(button(label,()=>decision(thread,choice)));
 root.append(button('Refresh status',reloadAll),button('Recover saved decision',async()=>{
 const value=await api('/threads/'+thread.review.proposal.thread_id+'/recover','POST',{});status(outcomeLabel(value));await reloadAll();}));
 root.append(button('Audit history',async()=>{const values=await api('/threads/'+thread.review.proposal.thread_id+'/history');root.append(disclose('Audit events',values));}));}
function renderCard(root,card){root.append(text('p',card.text));
 if(card.choices?.length){const choices=document.createElement('div');choices.className='choice-list';
 for(const choice of card.choices){if(canSelect(card,conversation)&&choice.selection.kind===conversation.awaiting){choices.append(button(choice.label,()=>send('Select',choice.selection)));}
 else choices.append(text('p',choice.label));}root.append(choices);
 }else if(emptyResults(card))root.append(text('p','No items in this page'));
 if(card.payload?.next_offset!==null && card.payload?.next_offset!==undefined){root.append(button('Next results',()=>send(card.kind==='selection'?({analyze:'Analyze job',prepare:'Prepare my application',missing:'What information is missing?'})[conversation.pending]:card.kind==='jobs'?'Show my jobs':card.kind==='applications'?'Show my applications':'Show my pending approvals',null,card.payload.next_offset)));}
 if(card.kind==='missing')for(const q of card.payload.clarifications||[])root.append(text('p','? '+q.question));
 if(['analysis','missing'].includes(card.kind)){const {analysis,job,profile}=card.payload;
 if(analysis){root.append(text('strong','Rule decision: '+analysis.decision));
 const findings=card.kind==='missing'?analysis.findings.filter(f=>f.outcome!=='met'):analysis.findings;
 if(!findings.length)root.append(text('p','No missing or gap requirements in this assessment.'));
 for(const f of findings){const r=job.requirements.find(r=>r.id===f.requirement_id);const item=document.createElement('article');item.append(text('h4',r?.description||f.requirement_id),text('p',`${f.outcome} · ${f.importance} · ${f.reason}`),disclose('Evidence',evidenceForFinding(f,profile,r)));root.append(item);}
 }}
 if(card.kind==='preparation'){const p=card.payload;if(p.status==='drafts_created'){
 for(const d of p.drafts){root.append(text('h4',d.kind+' · review required'),text('pre',d.content),disclose('Changes',d.changes));}
 root.append(disclose('Practice plan, explanations & clarification questions',p.record.intelligence));
 }else root.append(disclose('Preparation result',p));}
 if(card.kind==='applications')for(const item of card.payload.items||[])root.append(text('p',item.status+' · '+item.id));
 if(card.kind==='approvals')for(const thread of card.payload.items||[])approvalCard(root,thread);
 for(const ref of card.references||[]){const target={candidate:'profile',job:'jobs',assessment:'analysis',preparation:'drafts',document:'drafts',version:'drafts',application:'tracker',thread:'approval'}[ref.kind];
 root.append(text('small',ref.kind+' · '+ref.id),detailLink('Open workspace details',target));
 if(['job','candidate','assessment','version'].includes(ref.kind)){const route={job:'jobs',candidate:'profiles',assessment:'assessments',version:'versions'}[ref.kind];root.append(button('Inspect '+ref.kind,async()=>{const result=await api('/'+route+'/'+ref.id);root.append(disclose('Resource details',result));}));}}
}
function render(){const root=$('transcript');root.replaceChildren();root.dir='ltr';
 if(!messages.length){root.classList.add('empty-transcript');const welcome=text('div',runtime?.mode==='llm'?'Ask about your career, jobs or drafts.':'Start with a suggested command. No model or external search is active.');welcome.className='empty-state';root.append(welcome);}else root.classList.remove('empty-transcript');
 if(runtime?.error)root.append(text('p',runtime.error));
 for(const message of messages){const item=document.createElement('article');item.className='chat-message '+message.role;item.dir='ltr';item.append(text('small',messageLabel(message)));
 if(message.card)renderCard(item,message.card);else item.append(text('p',message.content));for(const card of message.cards||[])renderCard(item,card);
 if(message.mode==='llm')item.append(text('small','Model advice is unverified. Structured cards show actual outcomes.'));root.append(item);}
 for(const operation of operations.filter(op=>op.agent_metadata?.steps?.length)){const summary=operation.agent_metadata;root.append(disclose('Recorded tool execution',{model_calls:summary.model_calls,steps:summary.steps.map(s=>({name:s.name,status:s.status,effect:s.effect})),error:summary.error_code}));}
 for(const operation of operations.filter(op=>['running','recoverable'].includes(op.status))){const item=document.createElement('article');item.append(text('p',operation.mode==='llm'?'Saved agent operation. Refresh status; tools are not blindly replayed.':'Saved operation awaiting completion.'),button('Continue operation',()=>refreshOperation(operation)));root.append(item);}

 if(retryRequest?.conversationId===conversation?.id)root.append(button('Retry same message',()=>submit(retryRequest.request)));
 root.lastElementChild?.scrollIntoView({block:'nearest'});
}
