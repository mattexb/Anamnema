"""Real loopback HTTP transport and model-file bounds, not a live model server."""

import json

import pytest

from anamnema.extraction_config import extraction_config
from anamnema.llama_provider import LlamaProvider, hash_model
from anamnema.setup_config import Endpoint, SetupError


def provider(service, **settings):
    config = extraction_config().model_copy(update=settings)
    return LlamaProvider(Endpoint(host="127.0.0.1", port=service.port), config, service.model_file)


def test_identity_rechecks_reject_changed_model_and_metadata(model_service):
    model = provider(model_service)
    model_service.context += 1
    with pytest.raises(SetupError, match="identity changed"):
        model.verify_model()
    model_service.context -= 1
    model_service.model_file.write_bytes(b"changed")
    with pytest.raises(SetupError, match="file changed"):
        model.verify_model()


def test_optional_model_token_is_only_a_header_not_identity_or_output(model_service):
    token = "fixture-" + "private-value"
    model = LlamaProvider(
        Endpoint(host="127.0.0.1", port=model_service.port),
        extraction_config(),
        model_service.model_file,
        token,
    )
    assert model_service.headers[0]["Authorization"] == "Bearer " + token
    assert token not in json.dumps(model.identity)


@pytest.mark.parametrize("token", ["unsafe\r\nInjected: value", "unsafe\tvalue", "nonascii-\u0100"])
def test_invalid_model_tokens_fail_before_transport_without_echo(model_service, token):
    with pytest.raises(SetupError, match="invalid bearer-header") as caught:
        LlamaProvider(
            Endpoint(host="127.0.0.1", port=model_service.port),
            extraction_config(),
            model_service.model_file,
            token,
        )
    assert token not in str(caught.value)
    assert model_service.requests == []


@pytest.mark.parametrize(
    "props",
    [
        {},
        {"model_path": "/not/a/model", "model_alias": "m", "build_info": "x"},
    ],
)
def test_missing_or_unverifiable_model_identity_fails(model_service, props):
    model_service.overrides["/props"] = (200, props)
    with pytest.raises(SetupError):
        provider(model_service)


@pytest.mark.parametrize(
    "route,response",
    [
        ("/apply-template", {"prompt": 12}),
        ("/tokenize", {"tokens": [True]}),
        ("/v1/chat/completions", {"choices": []}),
        (
            "/v1/chat/completions",
            {"choices": [{"finish_reason": "length", "message": {"content": "x"}}]},
        ),
    ],
)
def test_invalid_template_token_or_answer_responses_are_visible(model_service, route, response):
    model = provider(model_service)
    model_service.overrides[route] = (200, response)
    with pytest.raises(SetupError):
        model.generate({"messages": [{"role": "user", "content": "fixture"}], "max_tokens": 20})


def test_transport_refuses_redirects_error_bodies_and_size_overruns(model_service):
    model = provider(model_service)
    model_service.overrides["/error"] = (307, b"never print private content")
    with pytest.raises(SetupError, match="HTTP 307") as caught:
        model.call("GET", "/error")
    assert "private content" not in str(caught.value)
    assert [path for _, path, _ in model_service.requests].count("/error") == 1
    model.config = model.config.model_copy(update={"max_response_bytes": 8})
    with pytest.raises(SetupError, match="response exceeds"):
        model.call("GET", "/props")
    model.config = model.config.model_copy(update={"max_request_bytes": 8})
    before = len(model_service.requests)
    with pytest.raises(SetupError, match="request exceeds"):
        model.call("POST", "/chat", {"too_big": "fixture"})
    assert len(model_service.requests) == before


def test_timeout_and_invalid_json_do_not_leak_response_content(model_service):
    model = provider(model_service)
    model_service.overrides["/broken"] = (200, b"private malformed content")
    with pytest.raises(SetupError, match="ValidationError") as caught:
        model.call("GET", "/broken")
    assert "private malformed" not in str(caught.value)
    model_service.overrides["/nonfinite"] = (200, {"value": float("nan")})
    with pytest.raises(SetupError, match="transport failed"):
        model.call("GET", "/nonfinite")
    model.config = model.config.model_copy(update={"timeout_seconds": 0.01})
    model_service.chat_delay = 0.1
    with pytest.raises(SetupError, match="TimeoutError"):
        model.call(
            "POST", "/v1/chat/completions", {"messages": [{"content": "[#0 Owner] fixture"}]}
        )


def test_model_hash_bounds_and_symlink_are_refused(tmp_path):
    path = tmp_path / "model"
    path.write_bytes(b"fixture model")
    config = extraction_config()
    assert len(hash_model(path, config)) == 64
    with pytest.raises(SetupError, match="size bounds"):
        hash_model(path, config.model_copy(update={"max_model_bytes": 1}))
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(SetupError, match="nonsymlink"):
        hash_model(link, config)
    with pytest.raises(SetupError):
        hash_model(tmp_path / "missing", config)


@pytest.mark.parametrize(
    "change",
    [
        {"timeout_seconds": float("inf")},
        {"props_path": "//other-host/path"},
        {"chat_path": "/bad\r\nheader"},
        {"attempts_collection": "notes_chunks"},
    ],
)
def test_invalid_execution_policy_is_rejected(tmp_path, change):
    config = extraction_config().model_dump(mode="json")
    config.update(change)
    path = tmp_path / "execution.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SetupError, match="cannot be read or validated"):
        extraction_config(path)
