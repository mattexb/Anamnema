"""Real model HTTP transport with synthetic card answers, not a model-quality claim."""

import json

import pytest
from extraction_fixtures import completion

from anamnema.card_policy import card_policy
from anamnema.card_writer import CardWriter
from anamnema.extraction_config import extraction_config
from anamnema.llama_provider import LlamaProvider
from anamnema.setup_config import Endpoint, SetupError


@pytest.fixture
def writer(model_service):
    provider = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
    )
    return CardWriter(provider, card_policy()), model_service


def test_checked_card_labels_caps_and_missing_pin(writer):
    subject, server = writer
    subject.policy = subject.policy.model_copy(update={"statements": 2, "front": 1, "who": 1})
    server.chat_replies.append(
        completion(
            json.dumps(
                {
                    "statements": [
                        {"text": "  First  fact ", "notes": ["n1", "unknown"], "front": True},
                        {"text": "Second fact", "notes": ["n1"], "front": True},
                        {"text": "Third fact", "notes": ["n1"], "front": False},
                        {"text": "Unsupported", "notes": ["unknown"], "front": False},
                    ]
                }
            )
        )
    )
    result = subject.write(
        {"name": "example", "type": "project"},
        [
            {"id": "note-a", "text": "Original fact"},
            {"id": "note-b", "text": "Pinned fact", "pinned": True},
        ],
        [],
    )
    assert [line["text"] for line in result.statements] == ["First fact", "Second fact"]
    assert [line["front"] for line in result.statements] == [True, False]
    assert result.statements[0]["notes"] == ["note-a"]
    assert len(result.problems) == 5
    assert result.model == subject.identity
    assert server.calls[-1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_truthy_front_is_rejected_and_empty_card_makes_no_generation(writer):
    subject, server = writer
    assert subject.write({"name": "example"}, [], []).statements == []
    assert server.calls == []
    server.chat_replies.append(
        completion(
            json.dumps({"statements": [{"text": "Fact", "notes": ["n1"], "front": "false"}]})
        )
    )
    with pytest.raises(SetupError, match="invalid structured answer"):
        subject.write({"name": "example"}, [{"id": "a", "text": "Fact"}], [])
