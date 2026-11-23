"""Focused policy/request refusals without starting stores or models."""

import json

import pytest
from pydantic import ValidationError

from anamnema.lesson_operations import LessonRequest
from anamnema.lesson_policy import LessonPolicy, lesson_policy
from anamnema.setup_config import SetupError


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_types": []},
        {"severities": ["normal", "normal"]},
        {"word_kinds": ["not-configured"]},
        {"concepts": {" ": "Definition"}},
        {"model_options": {"messages": []}},
    ],
)
def test_lesson_policy_rejects_ambiguous_vocabulary_and_request_overrides(changes):
    value = {**lesson_policy().model_dump(mode="json"), **changes}
    with pytest.raises(ValidationError):
        LessonPolicy.model_validate_json(json.dumps(value))


def test_lesson_policy_missing_or_corrupt_file_fails_visibly(tmp_path):
    with pytest.raises(SetupError):
        lesson_policy(tmp_path / "missing.json")
    path = tmp_path / "invalid.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(SetupError):
        lesson_policy(path)


@pytest.mark.parametrize(
    "value",
    [
        {"operation": "build"},
        {"operation": "build", "notes": ["n"], "lesson": "l"},
        {"operation": "read"},
        {"operation": "read", "lesson": "l", "notes": ["n"]},
        {"operation": "rebuild", "lesson": "l", "historical": True},
        {"operation": "against", "lesson": "l"},
        {"operation": "read", "lesson": "l", "note": "n"},
        {"operation": "build", "notes": ["n"], "cwd": "/work"},
        {"operation": "read", "lesson": "l", "historical": "false"},
    ],
)
def test_incoherent_requests_are_refused_before_infrastructure(value):
    with pytest.raises(ValidationError):
        LessonRequest.model_validate_json(json.dumps(value))
