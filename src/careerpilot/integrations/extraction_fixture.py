"""One explicit synthetic fixture for offline extraction; not a general text parser."""

MOCK_JOB_TEXT = """Title: Python Developer
Employer: Example Organization
Required skill: Python (intermediate).
Preferred skill: SQL.
Required qualification: Computing degree or equivalent experience.
Experience: at least 3 years of backend development.
English B2 required.
Location: Berlin.
Arrangement: remote.
Arrangement: onsite.
Employment: full-time.
"""


def _present(value: object, quote: str) -> dict[str, object]:
    return {"state": "present", "value": value, "evidence": [{"quote": quote}], "alternatives": []}


def _missing() -> dict[str, object]:
    return {"state": "missing", "value": None, "evidence": [], "alternatives": []}


def mock_extraction_payload() -> dict[str, object]:
    """Return fresh containers so a caller cannot mutate later fixture responses."""
    return {
        "title": _present("Python Developer", "Title: Python Developer"),
        "employer": _present("Example Organization", "Employer: Example Organization"),
        "skills": [
            {
                "name": _present("Python", "Required skill: Python (intermediate)."),
                "minimum_proficiency": _present(
                    "intermediate", "Required skill: Python (intermediate)."
                ),
                "mandatory": _present(True, "Required skill: Python (intermediate)."),
            },
            {
                "name": _present("SQL", "Preferred skill: SQL."),
                "minimum_proficiency": _missing(),
                "mandatory": _present(False, "Preferred skill: SQL."),
            },
        ],
        "qualifications": [
            {
                "description": _present(
                    "Computing degree or equivalent experience",
                    "Required qualification: Computing degree or equivalent experience.",
                ),
                "mandatory": _present(
                    True, "Required qualification: Computing degree or equivalent experience."
                ),
            }
        ],
        "experience": [
            {
                "description": _present(
                    "backend development", "Experience: at least 3 years of backend development."
                ),
                "minimum_years": _present(
                    3.0, "Experience: at least 3 years of backend development."
                ),
                "mandatory": _missing(),
            }
        ],
        "languages": [
            {
                "name": _present("English", "English B2 required."),
                "minimum_level": _present("B2", "English B2 required."),
                "mandatory": _present(True, "English B2 required."),
            }
        ],
        "location": _present("Berlin", "Location: Berlin."),
        "work_arrangement": {
            "state": "conflicting",
            "value": None,
            "evidence": [{"quote": "Arrangement: remote."}, {"quote": "Arrangement: onsite."}],
            "alternatives": ["remote", "onsite"],
        },
        "employment_type": _present("full_time", "Employment: full-time."),
        "warnings": ["contradictory_information"],
    }
