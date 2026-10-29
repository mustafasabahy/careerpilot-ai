import {reviewState, showText, decisionPayload, evidenceForFinding} from './logic.mjs';
const $ = id => document.getElementById(id);
const state = {profiles: [], jobs: [], applications: [], preferences: [], assessment: null, preparation: null, thread: null};
let targets;
function status(text, kind='completed') { if(kind==='completed'&&/unknown|failed|invalidated/.test(text))kind='error'; $('status').textContent = text; $('status').dataset.state = kind; }
export async function api(path, method='GET', data) {
  const response = await fetch('/api'+path, {method, headers: {Authorization:'Bearer '+$('token').value,
    ...(data ? {'Content-Type':'application/json'} : {})}, body:data ? JSON.stringify(data) : undefined});
  const value = await response.json();
  if (!response.ok) { const error=new Error(value.error?.message || value.detail || 'Request failed'); error.status=response.status; throw error; }
  return value.data;
}
function bind(id, operation) { $(id).addEventListener('click', async () => {
  const button = $(id); button.disabled = true; status('Working — waiting for the server response.', 'loading');
  try { if(id!=='connect'&&!state.connected)throw Error('Connect to your workspace first.'); await operation(); } catch(error) { status(error.message, 'error'); }
  finally { button.disabled = false; updateApproval(); }
}); }
function select(id, values, label) { const old=$(id).value; $(id).replaceChildren();
  for (const value of values) { const option=showText(document,'option',label(value)); option.value=value.id; $(id).append(option); }
  if (values.some(v=>v.id===old)) $(id).value=old;
}
function showProfile() { const p=state.profiles.find(p=>p.id===$('profiles').value); $('profile-json').textContent=p?JSON.stringify(p,null,2):'No profile yet.';
  if(p){$('name').value=p.display_name.value||''; $('skills').value=p.skills.map(s=>s.name+(s.proficiency.value?':'+s.proficiency.value:'')).join(', ');} }
function cards(id, items, render) { const root=$(id); root.replaceChildren(); if(!items.length)root.append(showText(document,'p','No results.'));
  for(const item of items){const card=document.createElement('article'); render(card,item); root.append(card);} }
async function refreshJobs(){ const page=await api('/jobs?query='+encodeURIComponent($('query').value)); state.jobs=page.items;
  select('jobs-select',state.jobs,j=>j.title+' · '+j.employer);
  cards('job-cards',state.jobs,(card,j)=>{card.append(showText(document,'h3',j.title),showText(document,'p',j.employer),showText(document,'p',j.requirements.length+' declared requirements'),showText(document,'p','Source: '+j.source.name));}); }
async function refreshApplications(){state.applications=(await api('/applications')).items; select('applications',state.applications,a=>a.status+' · '+a.id.slice(0,8));
  cards('application-cards',state.applications,(c,a)=>{c.append(showText(document,'h3',a.status),showText(document,'p','Application '+a.id),showText(document,'p','Job '+a.job_id));});}
async function refresh(){state.profiles=(await api('/profiles')).items;select('profiles',state.profiles,p=>p.display_name.value||'Unnamed profile');showProfile();
  await refreshJobs();await refreshApplications();state.preferences=await api('/preferences');$('preferences').textContent=JSON.stringify(state.preferences,null,2);targets=await api('/targets');state.threads=(await api('/threads')).items;select('threads',state.threads.map(t=>({id:t.review.proposal.thread_id,thread:t})),t=>t.thread.review.status+' · '+t.id.slice(0,8));if(state.threads.length){state.thread=state.threads[0];updateApproval();}status('Workspace loaded.');}
function updateApproval(){const info=reviewState(state.thread);$('approval-state').textContent=info.label;
  for(const id of ['approve','reject','revise'])$(id).disabled=!info.canDecide;
  if(state.thread){$('proposal').textContent=JSON.stringify(state.thread.review,null,2);$('revised-content').value=JSON.stringify(state.thread.review.proposal.content,null,2);} }
async function stage(content, previous=null){if(!$('applications').value)throw Error('Select an application first.');
  state.thread=await api('/applications/'+$('applications').value+'/proposals','POST',{content,previous_proposal_id:previous});updateApproval();status('Action prepared. Waiting for explicit review.');location.hash='approval';}
async function decide(choice){if(!state.thread)throw Error('No action selected.');
  if(choice==='approve'&&!confirm('Approve this exact target, content, version and hash? External sending is simulated.'))return;
  const p=state.thread.review.proposal; state.thread=await api('/threads/'+p.thread_id+'/decision','POST',decisionPayload(p,choice,$('revision-reason').value));
  updateApproval();await refreshApplications();status('Server outcome: '+state.thread.review.status,['unknown','failed','invalidated'].includes(state.thread.review.status)?'error':'completed');}
bind('connect',async()=>{const me=await api('/me');$('account').textContent='Connected · '+me.id.slice(0,8);state.connected=true;await refresh();window.dispatchEvent(new Event('workspace-connected'));});
bind('seed',async()=>{await api('/seed','POST',{});await refresh();window.dispatchEvent(new Event('workspace-seeded'));});bind('refresh',refresh);bind('search',async()=>{await refreshJobs();status('Local search complete.');});
$('profiles').addEventListener('change',showProfile);
$('threads').addEventListener('change',()=>{state.thread=state.threads.find(t=>t.review.proposal.thread_id===$('threads').value);updateApproval();});
bind('save-profile',async()=>{if(!$('confirmed').checked)throw Error('Explicit confirmation is required.');
  const evidence=[{origin:'user_statement',reference:'authenticated-user:profile-form'}];
  const profile={display_name:{state:'known',value:$('name').value,evidence},skills:$('skills').value.split(',').map(s=>s.trim()).filter(Boolean).map(s=>{const [name,level]=s.split(':');return {name,evidence,...(level?{proficiency:{state:'known',value:level.trim(),evidence}}:{})};})};
  await api('/profiles','POST',{profile,explicitly_confirmed:true});await refresh();});
bind('save-preferences',async()=>{if(!$('confirm-preferences').checked)throw Error('Explicit confirmation is required.');
  const values=[{kind:'job_preferences',roles:$('roles').value.split(',').map(s=>s.trim()).filter(Boolean),skills:[]},
    {kind:'location_preferences',locations:[$('location').value].filter(Boolean),arrangements:[$('arrangement').value]},
    ...($('goal').value?[{kind:'career_goal',description:$('goal').value}]:[])];
  for(const value of values){const old=state.preferences.find(p=>p.value.kind===value.kind);await api('/preferences','PUT',{item:{id:old?.id||crypto.randomUUID(),expected_version:old?.version||0,value},explicitly_confirmed:true});}await refresh();});
bind('import-job',async()=>{await api('/jobs/import','POST',JSON.parse($('job-import').value));await refreshJobs();status('Posting imported.');});
bind('analyze',async()=>{if(!$('profiles').value||!$('jobs-select').value)throw Error('Choose a profile and job.');
  state.assessment=await api('/analysis','POST',{id:crypto.randomUUID(),created_at:new Date().toISOString(),candidate_id:$('profiles').value,job_id:$('jobs-select').value});
  const root=$('assessment');root.replaceChildren(showText(document,'h3','Mandatory rule decision: '+(state.assessment.decision||state.assessment.status)));for(const finding of state.assessment.findings||[]){const criterion=state.assessment.criteria.find(c=>c.requirement_id===finding.requirement_id);const job=state.jobs.find(j=>j.id===state.assessment.job_id);const requirement=job?.requirements.find(r=>r.id===finding.requirement_id);const card=document.createElement('article');card.append(showText(document,'h3',requirement?.description||'Requirement'),showText(document,'p',finding.outcome+' · '+(criterion?.importance||'unspecified')),showText(document,'p',finding.reason));const detail=document.createElement('details');detail.append(showText(document,'summary','Inspect supporting evidence'),showText(document,'pre',JSON.stringify(evidenceForFinding(finding,state.profiles.find(p=>p.id===state.assessment.candidate_id),requirement),null,2)));card.append(detail);root.append(card);}status('Assessment: '+state.assessment.status);location.hash='analysis';});
bind('prepare',async()=>{const a=state.assessment;if(!a?.assessment_id)throw Error('Run an assessment first.');
  const p=await api('/preparations','POST',{id:crypto.randomUUID(),created_at:new Date().toISOString(),candidate_id:a.candidate_id,job_id:a.job_id,assessment_id:a.assessment_id});state.preparation=p;
  const root=$('draft-content');root.replaceChildren();
  if(p.status==='drafts_created'){for(const draft of p.drafts){const card=document.createElement('article');card.append(showText(document,'h3',draft.kind+' · review required'),showText(document,'pre',draft.content));const details=document.createElement('details');details.append(showText(document,'summary','Review changes'),showText(document,'pre',draft.changes));card.append(details);root.append(card);}
    $('practice').textContent=p.record.intelligence.practice.sessions.map(s=>s.order+'. '+s.focus+' ('+s.duration_minutes+' min) — '+s.deliverable).join('\n\n')+'\n\nInterview questions\n'+p.record.intelligence.practice.questions.map(q=>'• '+q.prompt).join('\n');const details=document.createElement('details');details.append(showText(document,'summary','Full explanation, facts and clarification questions'),showText(document,'pre',JSON.stringify(p.record.intelligence,null,2)));$('assessment').append(details);
  }else{root.append(showText(document,'pre',JSON.stringify(p,null,2)));}status('Preparation: '+p.status);location.hash='drafts';});
bind('create-application',async()=>{const p=state.preparation;if(p?.status!=='drafts_created')throw Error('Prepare a CV first.');
  const d=p.record.documents.find(d=>d.kind==='resume');await api('/applications','POST',{candidate_id:p.record.candidate_id,job_id:p.record.job_id,document_version_id:d.version_id});await refreshApplications();status('Application tracked as a draft.');});
bind('change-status',()=>stage({kind:'change_application',target_status:$('target-status').value,reason:'Explicit user-requested status change'}));
bind('propose-message',()=>stage({kind:'recruiter_message',target_id:targets.recruiter.id,subject:$('subject').value,body:$('message').value}));
bind('propose-submission',async()=>{const a=state.applications.find(a=>a.id===$('applications').value);if(!a?.document_version_id)throw Error('Select an application with a resume.');const v=await api('/versions/'+a.document_version_id);const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(v.content));const hash=Array.from(new Uint8Array(digest)).map(v=>v.toString(16).padStart(2,'0')).join('');await stage({kind:'submit_application',target_id:targets.destination.id,document_version_id:v.id,document_sha256:hash});});
for(const [id,choice] of [['approve','approve'],['reject','reject'],['revise','revise']])bind(id,()=>decide(choice));
bind('refresh-thread',async()=>{if(!state.thread)throw Error('No thread selected.');state.thread=await api('/threads/'+state.thread.review.proposal.thread_id);updateApproval();status('Workflow state refreshed.');});
bind('recover-thread',async()=>{if(!state.thread)throw Error('No thread selected.');state.thread=await api('/threads/'+state.thread.review.proposal.thread_id+'/recover','POST',{});updateApproval();status('Recovery result: '+state.thread.review.status);});
bind('cancel-thread',async()=>{if(!state.thread)throw Error('No thread selected.');await api('/threads/'+state.thread.review.proposal.thread_id+'/cancel','POST',{});state.thread=await api('/threads/'+state.thread.review.proposal.thread_id);updateApproval();status('Cancellation state: '+state.thread.review.status);});
bind('history',async()=>{if(!state.thread)throw Error('No thread selected.');$('audit').textContent=JSON.stringify(await api('/threads/'+state.thread.review.proposal.thread_id+'/history'),null,2);status('Audit loaded.');});
bind('new-revision',()=>{if(state.thread?.review.status!=='revision_requested')throw Error('Request revision first.');return stage(JSON.parse($('revised-content').value),state.thread.review.proposal.id);});
