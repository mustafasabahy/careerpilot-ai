"""Paid smoke test: excluded by default even if credentials happen to exist."""

import os

import pytest
from test_extraction import extraction_request

from careerpilot.application.extraction import ExtractionService
from careerpilot.domain.extraction import ExtractionState
from careerpilot.providers.factory import build_job_model
from careerpilot.providers.settings import LLMSettings


@pytest.mark.live_llm
def test_live_job_extraction() -> None:
    if os.environ.get("CAREERPILOT_RUN_LIVE_TESTS") != "1":
        pytest.fail("Explicit CAREERPILOT_RUN_LIVE_TESTS=1 is required for paid live testing")
    settings = LLMSettings()
    if settings.provider != "openai":
        pytest.fail(
            "Select CAREERPILOT_LLM_PROVIDER=openai and configure model/credential explicitly"
        )
    result = ExtractionService(build_job_model(settings)).extract(extraction_request())
    assert result.provenance.provider == "openai"
    assert result.provenance.trust == "unreviewed_inference"
    assert result.extraction.title.state == ExtractionState.PRESENT
    assert result.extraction.title.value == "Python Developer"
