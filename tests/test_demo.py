"""The executable demo exercises the actual graph against a temporary SQLite file."""

import json
from pathlib import Path

import pytest

from careerpilot.demo import main


@pytest.mark.parametrize(
    ("scenario", "status", "decision"),
    [
        ("complete", "completed", "eligible"),
        ("missing-information", "needs_information", "needs_review"),
        ("hard-gap", "completed", "ineligible"),
    ],
)
def test_demo_scenarios(
    scenario: str,
    status: str,
    decision: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["careerpilot-demo", "--scenario", scenario])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == status
    assert result["decision"] == decision


@pytest.mark.parametrize("content", ["{", "{}", "[]"])
def test_demo_invalid_input_has_nonzero_exit(
    content: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic-invalid.json"
    path.write_text(content)
    monkeypatch.setattr("sys.argv", ["careerpilot-demo", "--input", str(path)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert json.loads(capsys.readouterr().out) == {"status": "error", "code": "invalid_request"}
