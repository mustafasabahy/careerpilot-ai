import test from 'node:test';
import assert from 'node:assert/strict';
import {reviewState, showText, decisionPayload, evidenceForFinding} from '../../src/careerpilot/web/logic.mjs';
test('approval enabled only for actual paused pending state',()=>{
  assert.equal(reviewState(null).canDecide,false);
  for(const status of ['approved','simulated','unknown','applied','rejected'])assert.equal(reviewState({paused:true,review:{status}}).canDecide,false);
  assert.equal(reviewState({paused:true,review:{status:'pending'}}).canDecide,true);
});
test('untrusted strings are inserted as text, never HTML',()=>{
  const doc={createElement:tag=>({tag})};
  const result=showText(doc,'p','<script>stealToken()</script>');
  assert.equal(result.textContent,'<script>stealToken()</script>');assert.equal(result.innerHTML,undefined);
});
test('review decision binds the displayed immutable version',()=>{
  const payload=decisionPayload({id:'p',thread_id:'t',version:2,content_sha256:'abc'},'approve','User confirmed');
  assert.equal(payload.proposal_version,2);assert.equal(payload.content_sha256,'abc');assert.equal(payload.decision,'approve');
});

test('assessment evidence IDs resolve to their actual profile and source provenance',()=>{
  const profile={skills:[{evidence:[{id:'fact',origin:'user_statement',reference:'synthetic:confirmed'}]}]};
  const requirement={evidence:[{id:'job',origin:'source_document',reference:'synthetic:job'}]};
  assert.deepEqual(evidenceForFinding({evidence_ids:['fact','job']},profile,requirement),[
    {id:'fact',origin:'user_statement',reference:'synthetic:confirmed'},
    {id:'job',origin:'source_document',reference:'synthetic:job'}
  ]);
  assert.deepEqual(evidenceForFinding({evidence_ids:['absent']},profile,requirement),[{id:'absent',status:'Source reference not loaded'}]);
});
