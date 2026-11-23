"""Real HTTP and CPU ONNX boundaries with explicit synthetic model/tokenizer fixtures."""

import json

import numpy as np
import onnx
import pytest
from fastapi.testclient import TestClient
from onnx import TensorProto, helper
from search_fixtures import SearchHTTP
from tokenizers import Tokenizer, models, pre_tokenizers

from anamnema.onnx_ranker import ONNXRanker, load_onnx
from anamnema.reranker_service import create_reranker_app
from anamnema.search_services import CPUEncoder, CPUReranker, LocalJSON, load_search_services
from anamnema.service_config import load_service
from anamnema.setup_config import Endpoint, SetupError


def connection(server, **changes):
    policy = load_search_services().model_copy(update={"dimensions": 4, **changes})
    return LocalJSON(
        Endpoint(host="127.0.0.1", port=server.server.server_port),
        policy,
        "fixture-search-token",
        1.0,
    )


def test_encoder_prefixes_exact_tokens_trim_and_reordered_indexes(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    with SearchHTTP() as server:
        encoder = CPUEncoder(connection(server, max_tokens=20))
        encoded = encoder.encode(["fourteen", "seven"], passage=True)
        assert encoded[0] == (1.0, 0.0, 0.0, 0.0)
        assert encoded[1] == (0.8, 0.6, 0.0, 0.0)
        assert encoder.within_tokens("x" * 50) == "x" * 18
        bodies = [body for path, body in server.calls if path == "/v1/embeddings"]
        assert bodies[0]["input"] == ["passage: fourteen", "passage: seven"]
        assert bodies[0]["model"] == "e5-small-v2-q8_0"
        assert any(path == "/detokenize" for path, _ in server.calls)


@pytest.mark.parametrize(
    "rows",
    [
        [{"index": 0, "embedding": [1, 0, 0, 0]}, {"index": 0, "embedding": [1, 0, 0, 0]}],
        [{"index": 0, "embedding": [1, 0]}],
        [{"index": 0, "embedding": [0, 0, 0, 0]}],
        [{"index": 0, "embedding": [True, 0, 0, 0]}],
        [{"index": -1, "embedding": [1, 0, 0, 0]}],
        [],
    ],
)
def test_encoder_refuses_malformed_vectors(rows):
    with SearchHTTP() as server:
        server.override["/v1/embeddings"] = {"data": rows}
        with pytest.raises(SetupError):
            CPUEncoder(connection(server)).encode(["fourteen"], passage=False)


def test_encoder_piece_offsets_cover_original_text():
    with SearchHTTP() as server:
        encoder = CPUEncoder(connection(server, input_characters=10, chunk_overlap=2))
        embedded = encoder.embed_note("x" * 27)
        assert [piece.offset for piece in embedded.pieces] == [8, 16, 24]
        assert len(embedded.query_vector) == 4


def test_subject_prefix_does_not_displace_the_original_character_budget():
    with SearchHTTP() as server:
        encoder = CPUEncoder(connection(server, input_characters=10, chunk_overlap=2))
        encoder.encode(["0123456789discarded"], passage=True, subjects=["fixture subject"])
        body = [body for path, body in server.calls if path == "/v1/embeddings"][-1]
        assert body["input"] == ["passage: fixture subject. 0123456789"]
        with pytest.raises(SetupError, match="one subject per passage"):
            encoder.encode(["text"], passage=False, subjects=["subject"])


def test_reranker_maps_raw_scores_by_index():
    with SearchHTTP() as server:
        ranker = CPUReranker(connection(server))
        assert ranker.scores("question", ["one", "two"]) == [0.0, 1.0]
        server.override["/v1/rerank"] = {
            "results": [{"index": 0, "relevance_score": 4.0}, {"index": 0, "relevance_score": 3.0}]
        }
        with pytest.raises(SetupError, match="duplicate"):
            ranker.scores("question", ["one", "two"])


@pytest.fixture
def onnx_ranker(tmp_path):
    unknown = "[UNK]"
    tokenizer = Tokenizer(
        models.WordLevel(
            {"[UNK]": 0, "[PAD]": 1, "question": 2, "one": 3, "two": 4}, unk_token=unknown
        )
    )
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "special_tokens_map.json").write_text(
        json.dumps({"pad_token": "[PAD]"}), encoding="utf-8"
    )
    inputs = [helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "tokens"])]
    outputs = [helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["batch", 1])]
    nodes = [
        helper.make_node("Cast", ["input_ids"], ["float_ids"], to=TensorProto.FLOAT),
        helper.make_node("ReduceSum", ["float_ids", "axes"], ["logits"], keepdims=1),
    ]
    axes = onnx.numpy_helper.from_array(np.array([1], dtype=np.int64), name="axes")
    graph = helper.make_graph(nodes, "synthetic-reranker-not-minilm", inputs, outputs, [axes])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)], ir_version=8)
    onnx.save(model, str(tmp_path / "model.onnx"))
    settings = load_onnx().model_copy(update={"model_alias": "synthetic-onnx", "max_length": 8})
    return ONNXRanker(tmp_path, settings)


def test_real_onnx_cpu_inference_and_authenticated_http(onnx_ranker):
    assert onnx_ranker.scores("question", ["one", "two"]) == [5.0, 6.0]
    settings = load_service()
    client = TestClient(
        create_reranker_app(onnx_ranker, settings, {"fixture": "fixture-token"}, "/v1/rerank")
    )
    body = {"query": "question", "documents": ["one", "two"]}
    assert client.post("/v1/rerank", json=body).status_code == 401
    response = client.post(
        "/v1/rerank", json=body, headers={"Authorization": "Bearer fixture-token"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"] == [
        {"index": 1, "relevance_score": 6.0},
        {"index": 0, "relevance_score": 5.0},
    ]
    with pytest.raises(SetupError, match="token limit"):
        onnx_ranker.scores("question " * 9, ["one"])


def test_missing_or_invalid_onnx_has_safe_error(tmp_path):
    with pytest.raises(SetupError, match="initialization"):
        ONNXRanker(tmp_path, load_onnx())
