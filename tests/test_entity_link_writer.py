"""Entity feed judgements through a bounded HTTP fixture, without graph mutation."""

import json

import pytest
from extraction_fixtures import completion

from anamnema.entity_link_policy import EntityLinkPolicy, entity_link_policy
from anamnema.entity_link_writer import EntityLinkWriter, RosterEntity
from anamnema.extraction_config import extraction_config
from anamnema.llama_provider import LlamaProvider
from anamnema.setup_config import Endpoint, SetupError


@pytest.fixture
def linker(model_service):
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    return EntityLinkWriter(provider, entity_link_policy()), model_service


def roster():
    return (
        RosterEntity(name="Device Alpha", type="machine", status="confirmed"),
        RosterEntity(name="Device Beta", type="machine", status="unconfirmed"),
    )


def answer(server, **links):
    server.chat_replies.append(completion(json.dumps({"links": links})))


def row(**changes):
    return {"entities": [], "ambiguous": [], "new": [], **changes}


def test_empty_excluded_and_secret_shaped_notes_never_generate(linker):
    writer, server = linker
    assert writer.judge([], ()).links == ()
    notes = [
        {"id": "rule", "text": "A general rule.", "kind": "rule"},
        {"id": "secret", "text": "Synthetic canary Demo1!xyz", "kind": "fact"},
        {"id": "masked", "text": "Credential [secret]", "kind": "fact"},
    ]
    before = [dict(note) for note in notes]
    result = writer.judge(notes, roster())
    assert len(result.links) == 3 and all(not item.entities for item in result.links)
    assert [item.text for item in result.links] == [note["text"] for note in notes]
    assert len(result.problems) == 2 and notes == before and not server.calls


def test_labels_resolve_and_proposals_are_deduplicated_without_creation(linker):
    writer, server = linker
    answer(
        server,
        n1=row(
            entities=["e1", "e1", "missing"],
            ambiguous=[{"name": "Device", "candidates": ["e2", "e1", "missing"]}],
            new=[{"name": "New Device", "type": "machine"}],
        ),
        n2=row(new=[{"name": "New Device", "type": "machine"}]),
    )
    result = writer.judge(
        [{"id": "one", "text": "Device."}, {"id": "two", "text": "New device."}], roster()
    )
    assert result.links[0].entities == ("Device Alpha",)
    assert [item.text for item in result.links] == ["Device.", "New device."]
    assert result.links[0].ambiguous[0].candidates == ("Device Alpha", "Device Beta")
    assert [item.proposed for item in result.links] == [("New Device",), ("New Device",)]
    assert len(result.proposals) == 1 and len(result.problems) == 1
    assert result.model == writer.identity
    body = server.calls[0]
    assert body["response_format"]["type"] == "json_schema"
    assert '"label":"n1"' in body["messages"][0]["content"]


def test_missing_answer_stays_pending_and_unknown_note_label_refuses(linker):
    writer, server = linker
    notes = [{"id": "one", "text": "A device."}]
    answer(server)
    result = writer.judge(notes, ())
    assert not result.links and "remains pending" in result.problems[0]
    answer(server, n9=row())
    with pytest.raises(SetupError, match="unknown note labels"):
        writer.judge(notes, ())


@pytest.mark.parametrize("bad", [{}, {"links": []}, {"links": {"n1": {"entities": "e1"}}}])
def test_invalid_structured_answers_have_no_fallback(linker, bad):
    writer, server = linker
    server.chat_replies.append(completion(json.dumps(bad)))
    with pytest.raises(SetupError, match="invalid structured answer"):
        writer.judge([{"id": "one", "text": "Device."}], ())


def test_unsafe_answers_drop_and_known_proposal_resolves(linker):
    writer, server = linker
    answer(
        server,
        n1=row(
            new=[
                {"name": "Device Alpha", "type": "machine"},
                {"name": "[secret]", "type": "machine"},
                {"name": "Demo1!xyz", "type": "machine"},
                {"name": "A Project", "type": "project"},
                {"name": " spaced ", "type": "machine"},
            ],
            ambiguous=[{"name": "Device", "candidates": ["e1", "missing"]}],
        ),
    )
    result = writer.judge([{"id": "one", "text": "Device."}], roster())
    assert result.links[0].entities == ("Device Alpha",)
    assert not result.proposals and not result.links[0].ambiguous
    assert len(result.problems) == 5


def test_conflicting_proposals_refuse_whole_result(linker):
    writer, server = linker
    answer(
        server,
        n1=row(new=[{"name": "Shared", "type": "person"}]),
        n2=row(new=[{"name": "Shared", "type": "machine"}]),
    )
    with pytest.raises(SetupError, match="disagree"):
        writer.judge([{"id": "one", "text": "First."}, {"id": "two", "text": "Second."}], ())


def test_roster_fields_are_scrubbed_and_secret_name_is_not_a_label(linker):
    writer, server = linker
    answer(server, n1=row())
    writer.judge(
        [{"id": "one", "text": "Device."}],
        (
            RosterEntity(name="Demo1!xyz", type="machine", status="confirmed"),
            RosterEntity(name="Safe", type="Demo1!xyz", aliases=("Demo1!xyz",), status="confirmed"),
        ),
    )
    prompt = server.calls[0]["messages"][0]["content"]
    assert "Demo1!xyz" not in prompt and '"label":"e2"' in prompt
    assert '"label":"e1"' not in prompt


def test_bounds_refuse_before_generation(linker):
    writer, server = linker
    note = {"id": "one", "text": "Device."}
    with pytest.raises(SetupError, match="repeats a note"):
        writer.judge([note, note], ())
    with pytest.raises(SetupError, match="canonical name"):
        writer.judge([note], (roster()[0], roster()[0]))
    writer.policy = writer.policy.model_copy(update={"max_roster_entities": 1})
    with pytest.raises(SetupError, match="roster exceeds"):
        writer.judge([note], roster())
    writer.policy = writer.policy.model_copy(update={"max_input_bytes": 1})
    with pytest.raises(SetupError, match="byte bound"):
        writer.judge([note], ())
    assert not server.calls


def test_policy_rejects_controlled_options_credentials_and_duplicate_types():
    defaults = entity_link_policy().model_dump(mode="json")
    for update in (
        {"model_options": {"max_tokens": 1}},
        {"model_options": {"headers": {"Authorization": "synthetic"}}},
        {"proposal_types": ["person", "person"]},
    ):
        with pytest.raises(ValueError):
            EntityLinkPolicy.model_validate_json(json.dumps({**defaults, **update}))
