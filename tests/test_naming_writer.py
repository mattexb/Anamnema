"""Source naming semantics through the real bounded HTTP provider, not a quality claim."""

import json

import pytest
from extraction_fixtures import completion

from anamnema.extraction_config import extraction_config
from anamnema.llama_provider import LlamaProvider
from anamnema.naming_policy import NamingPolicy, naming_policy
from anamnema.naming_writer import NamingWriter, cut_at_word
from anamnema.setup_config import Endpoint, SetupError


@pytest.fixture
def naming(model_service):
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    return NamingWriter(provider, naming_policy()), model_service


def test_empty_batch_has_no_generation_and_unknown_is_not_searchable(naming):
    writer, server = naming
    assert writer.judge([], {}).subjects == () and not server.calls
    server.chat_replies.append(
        completion(
            json.dumps(
                {"subjects": {"n1": {"known": False, "subject": "unknown should not be indexed"}}}
            )
        )
    )
    result = writer.judge([{"id": "note", "text": "A vague observation."}], {})
    assert not result.subjects[0].known and result.subjects[0].subject == ""
    assert result.model == writer.identity


@pytest.mark.parametrize("known", ["false", "true", 0, 1, None, [], {}])
def test_truthy_known_values_are_rejected(naming, known):
    writer, server = naming
    server.chat_replies.append(
        completion(
            json.dumps({"subjects": {"n1": {"known": known, "subject": "fixture activity"}}})
        )
    )
    with pytest.raises(SetupError, match="invalid structured answer"):
        writer.judge([{"id": "note", "text": "A fixture note."}], {})


def test_missing_or_empty_labels_remain_pending_and_unknown_labels_fail(naming):
    writer, server = naming
    writer.policy = writer.policy.model_copy(update={"batch": 2})
    notes = [{"id": "first", "text": "First note."}, {"id": "second", "text": "Second note."}]
    server.chat_replies.append(
        completion(json.dumps({"subjects": {"n1": {"known": True, "subject": ""}}}))
    )
    result = writer.judge(notes, {})
    assert not result.subjects and len(result.problems) == 2
    server.chat_replies.append(
        completion(json.dumps({"subjects": {"n9": {"known": True, "subject": "other"}}}))
    )
    with pytest.raises(SetupError, match="unknown note labels"):
        writer.judge(notes, {})


def test_subject_word_cap_and_credential_masking_do_not_change_the_note(naming):
    writer, server = naming
    writer.policy = writer.policy.model_copy(
        update={"subject_characters": 12, "subject_margin": 0.0}
    )
    note = {"id": "note", "text": "Owner gave Demo1!xyz as a synthetic canary."}
    original = dict(note)
    server.chat_replies.append(
        completion(
            json.dumps(
                {"subjects": {"n1": {"known": True, "subject": "router management interface"}}}
            )
        )
    )
    result = writer.judge([note], {"note": ["Owner: use the router."]})
    assert result.subjects[0].subject == "router" and result.problems
    assert note == original
    assert cut_at_word("oneunbrokenword", 5) == "oneun"
    server.chat_replies.append(
        completion(json.dumps({"subjects": {"n1": {"known": True, "subject": "Demo1!xyz"}}}))
    )
    assert writer.judge([note], {}).subjects[0].subject == "[secret]"


def test_naming_policy_rejects_credentials_and_controlled_options():
    defaults = naming_policy().model_dump(mode="json")
    for options in ({"max_tokens": 1}, {"headers": {"Authorization": "synthetic"}}):
        with pytest.raises(ValueError):
            NamingPolicy.model_validate_json(json.dumps({**defaults, "model_options": options}))
