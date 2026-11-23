"""Authenticated loopback JSON service for the bundled ONNX CPU inference implementation."""

from typing import Any

from fastapi import FastAPI

from anamnema.onnx_ranker import ONNXRanker
from anamnema.service_config import ServiceSettings
from anamnema.service_http import JsonService
from anamnema.setup_config import Nonempty, StrictModel


class RerankRequest(StrictModel):
    """Strict indexed reranking request in the existing service's wire format."""

    query: Nonempty
    documents: tuple[Nonempty, ...]


def create_reranker_app(
    ranker: ONNXRanker, settings: ServiceSettings, tokens: dict[str, str], path: str
) -> FastAPI:
    """Each request needs the service's own token, including its health endpoint."""
    service = JsonService(settings, tokens, "Anamnema CPU reranker")

    def rerank(instance: str, body: RerankRequest) -> dict[str, Any]:
        scores = ranker.scores(body.query, list(body.documents))
        order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
        return {
            "model": ranker.settings.model_alias,
            "object": "list",
            "results": [{"index": index, "relevance_score": scores[index]} for index in order],
        }

    service.post(
        path, RerankRequest, rerank, "Rank query/document pairs using CPU ONNX raw logits."
    )
    return service.app
