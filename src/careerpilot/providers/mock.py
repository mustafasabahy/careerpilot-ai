"""Strict offline fixture adapter implementing the same application port."""

from typing import Literal

from careerpilot.application.extraction import MockInputUnsupported
from careerpilot.integrations.extraction_fixture import MOCK_JOB_TEXT, mock_extraction_payload


class MockJobModel:
    provider: Literal["mock"] = "mock"
    model = "fixture-v1"

    def extract(self, text: str) -> object:
        from careerpilot.application.execution import consume

        consume("model_calls")
        if text != MOCK_JOB_TEXT:
            raise MockInputUnsupported("Mock mode accepts only the bundled synthetic job fixture")
        return mock_extraction_payload()
