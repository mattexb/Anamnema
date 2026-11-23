"""Actual response ownership on non-200 and bounded-read failures, without GC timing."""

import http.client

import pytest

from anamnema.extraction_config import extraction_config
from anamnema.llama_provider import LlamaProvider
from anamnema.search_services import LocalJSON, load_search_services
from anamnema.setup_config import Endpoint, SetupError


@pytest.mark.parametrize("family", ["notes", "search"])
@pytest.mark.parametrize("failure", ["status", "size"])
def test_early_http_failures_close_response_before_return(
    model_service, monkeypatch, family, failure
):
    responses = []
    original = http.client.HTTPConnection

    class ObservedConnection(original):
        def getresponse(self):
            response = super().getresponse()
            responses.append(response)
            return response

    monkeypatch.setattr(http.client, "HTTPConnection", ObservedConnection)
    endpoint = Endpoint(host="127.0.0.1", port=model_service.port)
    if family == "notes":
        client = LlamaProvider(endpoint, extraction_config(), model_service.model_file)
        if failure == "size":
            client.config = client.config.model_copy(update={"max_response_bytes": 8})

        def invoke(path):
            return client.call("GET", path)
    else:
        policy = load_search_services()
        if failure == "size":
            policy = policy.model_copy(update={"max_response_bytes": 8})
        client = LocalJSON(endpoint, policy, "fixture-token", 1.0)

        def invoke(path):
            return client.call(path, {})

    model_service.overrides["/reject"] = (403, {"error": "fixture rejection"})
    try:
        with pytest.raises(SetupError):
            invoke("/reject" if failure == "status" else "/props")
        assert responses and responses[-1].isclosed()
    finally:
        # Negative controls must not leak their observed responses into later tests.
        for response in responses:
            response.close()
