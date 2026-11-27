"""Real structured model transport with strict multi-placement and malformed-output checks."""

import copy
import json

import pytest
from extraction_fixtures import completion

from anamnema.extraction_config import extraction_config
from anamnema.lesson_policy import lesson_policy
from anamnema.lesson_writer import LessonAnswer, LessonWriter
from anamnema.llama_provider import LlamaProvider
from anamnema.notes_policy import notes_policy
from anamnema.setup_config import Endpoint, SetupError


def output():
    return {
        "lessons": {
            "L1": {
                "statement": "Check sources before action",
                "mechanism": "Guessing",
                "routes": [
                    {
                        "condition": "Before acting",
                        "correction": "Read n1",
                        "notes": ["n1"],
                        "exception": False,
                    }
                ],
                "concepts": [],
                "severity": "normal",
                "status": "reviewed",
                "conflict": "",
            }
        },
        "notes": {
            "n1": [{"lesson": "L1", "evidence": "ruling", "event": "event1", "event_reason": ""}]
        },
    }


def test_generation_schema_constrains_status_to_the_locally_accepted_values():
    status = LessonAnswer.model_json_schema()["$defs"]["JudgedContent"]["properties"]["status"]
    assert status["enum"] == ["reviewed", "conflict"]
    assert "settled" not in status["enum"]


def test_generation_schema_constrains_configured_vocabulary(writer):
    generator, model = writer
    generator.policy = generator.policy.model_copy(
        update={"evidence_types": ("ruling", "failure"), "severities": ("normal",)}
    )
    model.chat_replies.append(completion(json.dumps(output())))
    generator.judge("owner", notes(), [])
    definitions = model.calls[-1]["response_format"]["json_schema"]["schema"]["$defs"]
    assert definitions["PlacedEpisode"]["properties"]["evidence"]["enum"] == ["ruling", "failure"]
    assert definitions["JudgedContent"]["properties"]["severity"]["enum"] == ["normal"]


@pytest.fixture
def writer(model_service):
    policy = lesson_policy()
    model_service.context = policy.answer_tokens + model_service.token_count
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    return LessonWriter(provider, policy), model_service


def notes():
    return [
        {
            "note": "real-note",
            "source_ids": ["actual-record"],
            "text": "Check sources",
            "source": {
                "rows": [{"speaker": "owner", "text": "Read the original"}],
                "context": {"status": "available"},
            },
        }
    ]


def test_one_note_can_join_two_mechanisms_with_stable_separate_lesson_ids(writer):
    writer, model = writer
    body = output()
    body["lessons"]["L2"] = {**body["lessons"]["L1"], "mechanism": "Another mechanism"}
    body["notes"]["n1"].append({**body["notes"]["n1"][0], "lesson": "L2"})
    for _ in range(2):
        model.chat_replies.append(completion(json.dumps(body)))
    first, unresolved = writer.judge("owner", notes(), [])
    again, _ = writer.judge("owner", notes(), [])
    assert not unresolved and len(first) == 2
    assert first[0].id != first[1].id and [item.id for item in first] == [item.id for item in again]
    assert all(item.content.routes[0].notes == ("real-note",) for item in first)
    assert all(item.episodes[0].records == ("actual-record",) for item in first)


@pytest.mark.parametrize(
    "fault",
    [
        "boolean",
        "route",
        "concept",
        "severity",
        "status",
        "missing-note",
        "unknown-lesson",
        "evidence",
        "duplicate",
    ],
)
def test_bad_model_output_fails_visibly_instead_of_guessing(writer, fault):
    writer, model = writer
    body = output()
    lesson = body["lessons"]["L1"]
    if fault == "boolean":
        lesson["routes"][0]["exception"] = "false"
    elif fault == "route":
        lesson["routes"][0]["notes"] = ["n999"]
    elif fault == "concept":
        lesson["concepts"] = ["unknown"]
    elif fault == "severity":
        lesson["severity"] = "unknown"
    elif fault == "status":
        lesson["status"] = "settled"
    elif fault == "missing-note":
        body["notes"] = {}
    elif fault == "unknown-lesson":
        body["notes"]["n1"][0]["lesson"] = "E999"
    elif fault == "evidence":
        body["notes"]["n1"][0]["evidence"] = "unknown"
    else:
        body["notes"]["n1"].append(copy.deepcopy(body["notes"]["n1"][0]))
    model.chat_replies.append(completion(json.dumps(body)))
    with pytest.raises(SetupError):
        writer.judge("owner", notes(), [])


def test_unresolved_is_explicit_and_context_limit_failure_never_generates(writer):
    writer, model = writer
    model.chat_replies.append(completion(json.dumps({"lessons": {}, "notes": {"n1": []}})))
    result, unresolved = writer.judge("owner", notes(), [])
    assert result == [] and unresolved == ["real-note"]
    calls = len(model.calls)
    model.token_count = model.context
    with pytest.raises(SetupError, match="exceed the model context"):
        writer.judge("owner", notes(), [])
    assert len(model.calls) == calls


def test_existing_lesson_id_revision_and_in_text_labels_are_preserved(writer):
    writer, model = writer
    body = output()
    body["lessons"]["E1"] = body["lessons"].pop("L1")
    body["lessons"]["E1"]["statement"] = "n1 corrects E1"
    body["notes"]["n1"][0]["lesson"] = "E1"
    model.chat_replies.append(completion(json.dumps(body)))
    result, _ = writer.judge(
        "owner",
        notes(),
        [
            {
                "lesson_id": "existing-id",
                "revision": 7,
                "statement": "Old statement",
                "mechanism": "Guessing",
                "routes": [],
                "concepts": [],
                "severity": "normal",
                "status": "reviewed",
                "conflict": "",
            }
        ],
    )
    assert result[0].id == "existing-id" and result[0].revision == 7
    assert result[0].content.statement == "real-note corrects existing-id"


def test_shared_event_without_reasons_is_refused_before_it_can_be_embedded(writer):
    writer, model = writer
    body = output()
    body["notes"]["n2"] = copy.deepcopy(body["notes"]["n1"])
    model.chat_replies.append(completion(json.dumps(body)))
    second = {**notes()[0], "note": "second-note", "source_ids": ["second-record"]}
    with pytest.raises(SetupError, match="shared judged event"):
        writer.judge("owner", [*notes(), second], [])


def test_model_boundary_scrubs_notes_and_existing_content_not_only_source_rows(writer):
    writer, model = writer
    body = output()
    body["lessons"]["L1"].update({"nearest": "E1", "differs": "another correction"})
    model.chat_replies.append(completion(json.dumps(body)))
    raw = "password=NOT_A_REAL_PASSWORD"
    supplied = [{**notes()[0], "text": raw}]
    writer.judge("owner", supplied, [{"lesson_id": "existing", "revision": 1, "statement": raw}])
    prompt = model.calls[-1]["messages"][0]["content"]
    assert "NOT_A_REAL_PASSWORD" not in prompt and "redacted" in prompt


EXISTING = {
    "lesson_id": "existing-id",
    "revision": 3,
    "statement": "Run gates in the background",
    "mechanism": "Blocking the session",
    "routes": [],
    "concepts": [],
    "severity": "normal",
    "status": "reviewed",
    "conflict": "",
}


@pytest.mark.parametrize(
    "fields",
    [{}, {"nearest": "E1", "differs": " "}, {"nearest": "E7", "differs": "another scope"}],
)
def test_a_new_lesson_beside_existing_ones_must_say_how_it_differs(writer, fields):
    # One rule had splintered into fourteen lessons this way (lesson-delivery fix, 2026-11-27).
    writer, model = writer
    body = output()
    body["lessons"]["L1"].update(fields)
    model.chat_replies.append(completion(json.dumps(body)))
    with pytest.raises(SetupError, match="how it differs"):
        writer.judge("owner", notes(), [EXISTING])


def test_a_written_difference_is_accepted_and_never_stored(writer):
    writer, model = writer
    body = output()
    body["lessons"]["L1"].update({"nearest": "E1", "differs": "asks for a different correction"})
    model.chat_replies.append(completion(json.dumps(body)))
    result, _ = writer.judge("owner", notes(), [EXISTING])
    assert len(result) == 1
    assert "nearest" not in result[0].content.model_dump()
    assert "differs" not in result[0].content.model_dump()


def test_with_no_existing_lessons_a_new_one_needs_no_difference(writer):
    writer, model = writer
    model.chat_replies.append(completion(json.dumps(output())))
    result, _ = writer.judge("owner", notes(), [])
    assert len(result) == 1


def test_default_request_leaves_prompt_space_in_baseline_context(model_service):
    policy = lesson_policy()
    assert policy.answer_tokens + model_service.token_count < model_service.context
    assert policy.model_options == notes_policy().model_options
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    model_service.chat_replies.append(completion(json.dumps(output())))
    judged, _ = LessonWriter(provider, policy).judge("owner", notes(), [])
    assert len(judged) == 1
    body = model_service.calls[-1]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == policy.answer_tokens
