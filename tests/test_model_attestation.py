"""Remote claims are explicit, scoped and rechecked, never local model-file proof."""

import json

import pytest
from extraction_fixtures import completion

from anamnema.extraction_config import extraction_config
from anamnema.llama_provider import LlamaProvider
from anamnema.setup_config import Endpoint, SetupError

REQUEST = {"messages": [{"role": "user", "content": "synthetic request"}], "max_tokens": 100}


@pytest.fixture
def remote(model_service, tmp_path):
    props = {
        "model_path": "/synthetic-remote/models/notes.gguf",
        "model_alias": "fixture-model",
        "build_info": "fixture-build",
        "default_generation_settings": {"n_ctx": 8192},
    }
    model_service.overrides["/props"] = (200, props)
    path = tmp_path / "remote-attestation.json"
    document = {
        "schema_version": 1,
        "verification": "operator-attested-remote",
        "transport": "ssh-loopback",
        "ssh_host_key_sha256": "SHA256:" + "A" * 43,
        "endpoint": {"host": "127.0.0.1", "port": model_service.port},
        "model_path": props["model_path"],
        "sha256": "1" * 64,
        "alias": "fixture-model",
        "build": props["build_info"],
        "generation": props["default_generation_settings"],
    }
    path.write_text(json.dumps(document))
    endpoint = Endpoint(host="127.0.0.1", port=model_service.port)
    config = extraction_config().model_copy(update={"model_identity": "operator-attested-remote"})
    return path, document, endpoint, config, props, model_service


def test_attested_remote_generation_does_not_hash_a_local_model_or_claim_it(remote):
    path, document, endpoint, config, _, server = remote
    model = LlamaProvider(endpoint, config, path)
    assert model.identity["verification"] == "operator-attested-remote"
    assert model.identity["sha256"] == document["sha256"]
    assert "attestation" in model.identity
    server.chat_replies.append(completion("synthetic answer"))
    assert model.generate(REQUEST) == "synthetic answer"
    assert len([route for _, route, _ in server.requests if route == "/props"]) == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_path", "/other/model.gguf"),
        ("model_alias", "other"),
        ("build_info", "other-build"),
        ("default_generation_settings", {"n_ctx": 4096}),
    ],
)
def test_changed_remote_identity_refuses_before_generation(remote, field, value):
    path, _, endpoint, config, props, server = remote
    model = LlamaProvider(endpoint, config, path)
    server.overrides["/props"] = (200, {**props, field: value})
    with pytest.raises(SetupError, match="identity differs"):
        model.generate(REQUEST)
    assert not server.calls


def test_post_generation_change_and_changed_attestation_refuse(remote):
    path, _, endpoint, config, props, server = remote
    model = LlamaProvider(endpoint, config, path)
    server.chat_replies.append(completion("not accepted"))
    server.after_chat = lambda: server.overrides.update(
        {"/props": (200, {**props, "model_alias": "changed"})}
    )
    with pytest.raises(SetupError, match="identity differs"):
        model.generate(REQUEST)
    path.write_text("changed attestation")
    with pytest.raises(SetupError, match="file changed"):
        model.verify_model()


@pytest.mark.parametrize(
    "update",
    [
        {"transport": "plain-http"},
        {"sha256": "unpinned"},
        {"model_path": "relative.gguf"},
        {"ssh_host_key_sha256": "unpinned"},
        {"generation": {"n_ctx": True}},
        {"build": None},
        {"endpoint": {"host": "127.0.0.1", "port": 1}},
    ],
)
def test_bad_attestation_or_wrong_tunnel_refuses(remote, update):
    path, document, endpoint, config, _, server = remote
    path.write_text(json.dumps({**document, **update}))
    with pytest.raises(SetupError, match="attestation"):
        LlamaProvider(endpoint, config, path)
    assert not server.requests


def test_descriptor_is_bounded_and_never_auto_detected_as_remote(remote):
    path, _, endpoint, config, _, _ = remote
    with pytest.raises(SetupError, match="byte bound"):
        LlamaProvider(endpoint, config.model_copy(update={"max_attestation_bytes": 1}), path)
    with pytest.raises(SetupError):
        LlamaProvider(endpoint, extraction_config(), path)
