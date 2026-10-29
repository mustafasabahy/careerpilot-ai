"""Crash after a durable simulated effect, then recover in a different process."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_action_crash_after_effect_never_repeats_completed_steps(tmp_path: Path) -> None:
    script = """
import json,os,sys
from pathlib import Path
from careerpilot.approval_demo import compose
from careerpilot.domain.actions import ReviewDecision
from careerpilot.integrations.synthetic import fixture_id
from careerpilot.integrations.simulated_actions import SimulatedActionAdapter
from careerpilot.persistence.checkpoints import local_checkpointer
from careerpilot.workflows.action_approval import ActionApprovalWorkflow
root=Path(sys.argv[1]); service,request=compose(root,'message')
class CrashAdapter(SimulatedActionAdapter):
    def execute(self,proposal,key,*,timeout_seconds=30):
        super().execute(proposal,key,timeout_seconds=timeout_seconds)
        os._exit(0)
with local_checkpointer(root/'cp.sqlite3',separate_from=(root/'business.sqlite3',)) as saver:
    flow=ActionApprovalWorkflow(service,saver)
    if sys.argv[2]=='crash':
        waiting=flow.prepare(request)
        service.decide(ReviewDecision(id=fixture_id('crash-decision'),proposal_id=request.id,
          thread_id=request.thread_id,proposal_version=1,
          content_sha256=waiting.review.proposal.content_sha256,decision='approve'))
        service.adapter=CrashAdapter(root/'simulation.sqlite3')
        flow.recover(request.thread_id)
        raise AssertionError('Expected abrupt process exit')
    result=flow.recover(request.thread_id)
    flow.recover(request.thread_id)
    print(json.dumps({'status':result.review.status,'effects':service.adapter.effect_count(),
      'audit':[e.action for e in service.audit(request.thread_id)]}))
"""
    environment = {**os.environ, "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}
    args = [sys.executable, "-c", script, str(tmp_path)]
    first = subprocess.run(
        [*args, "crash"], capture_output=True, text=True, timeout=20, env=environment
    )
    assert first.returncode == 0, first.stderr
    second = subprocess.run(
        [*args, "recover"], capture_output=True, text=True, timeout=20, env=environment
    )
    assert second.returncode == 0, second.stderr
    result = json.loads(second.stdout)
    assert result == {
        "status": "unknown",
        "effects": 1,
        "audit": ["proposed", "approved", "execution_claimed", "unknown"],
    }
