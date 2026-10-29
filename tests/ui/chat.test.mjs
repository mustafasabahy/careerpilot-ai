import test from 'node:test';
import assert from 'node:assert/strict';
import {direction,canSelect,outcomeLabel,textBlock,approvalEnabled,prompts,emptyResults} from '../../src/careerpilot/web/chat-logic.mjs';
import {decisionPayload} from '../../src/careerpilot/web/logic.mjs';
test('English/Arabic direction follows content, not account identity',()=>{
 assert.equal(direction('Show my jobs'),'ltr');assert.equal(direction('حلل الوظيفة'),'rtl');
 assert.equal(prompts.length,7);assert.ok(prompts.every(p=>p.length===2));
});
test('resource choices require a pending clarification',()=>{
 assert.equal(canSelect({kind:'selection'},{pending:'analyze',awaiting:'job'}),true);
 assert.equal(canSelect({kind:'jobs'},{pending:'analyze',awaiting:'job'}),false);
 assert.equal(canSelect({kind:'selection'},{pending:null,awaiting:null}),false);
});
test('hostile transcript text is rendered literally using textContent',()=>{
 const element=textBlock({createElement:tag=>({tag})},'p','<img src=x onerror=steal()>');
 assert.equal(element.textContent,'<img src=x onerror=steal()>');assert.equal(element.innerHTML,undefined);assert.equal(element.dir,'auto');
});
test('simulation, approval and unknown outcomes have distinct labels and controls',()=>{
 const thread=status=>({paused:true,review:{status}});
 assert.match(outcomeLabel(thread('simulated')),/no external delivery/);
 assert.match(outcomeLabel(thread('unknown')),/do not retry/);
 assert.match(outcomeLabel(thread('approved')),/unproven/);
 for(const s of ['approved','applied','unknown','simulated','failed','invalidated'])assert.equal(approvalEnabled(thread(s)),false);
 assert.equal(approvalEnabled(thread('pending')),true);
});
test('chat approval events remain tied to the displayed immutable proposal',()=>{
 const p=decisionPayload({id:'proposal',thread_id:'thread',version:3,content_sha256:'hash'},'approve',null);
 assert.equal(p.thread_id,'thread');assert.equal(p.proposal_version,3);assert.equal(p.content_sha256,'hash');
});

test('empty state uses actual result items, not resource-choice controls',()=>{
 assert.equal(emptyResults({kind:'approvals',choices:[],payload:{items:[{review:{status:'pending'}}]}}),false);
 assert.equal(emptyResults({kind:'applications',payload:{items:[]}}),true);
 assert.equal(emptyResults({kind:'selection',choices:[]}),true);
});

test('mode indicators distinguish model, demo and missing configuration', async()=>{
 const {modeLabel,messageLabel}=await import('../../src/careerpilot/web/chat-logic.mjs');
 assert.match(modeLabel({mode:'demo',ready:true}),/No LLM/);
 assert.match(modeLabel({mode:'llm',ready:false}),/No demo fallback/);
 assert.match(modeLabel({mode:'llm',ready:true}),/LLM assistant/);
 assert.match(messageLabel({role:'assistant',mode:'llm'}),/LLM/);
 assert.match(messageLabel({role:'assistant'}),/demo/);
});
