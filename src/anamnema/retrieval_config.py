"""Reviewed retrieval policy, kept separate from service connection configuration."""

from importlib.resources import files
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from anamnema.setup_config import SetupError, StrictModel

Count = Annotated[int, Field(gt=0)]


class RetrievalSettings(StrictModel):
    """Source candidate fusion, paging and first-page reranking coefficients."""

    schema_version: Literal[1]
    vector_candidates: Count
    vector_oversample: Count
    text_candidates: Count
    rank_fusion_k: Count
    pool: Count
    meaning_pool: Count
    page: Count
    header_tokens: Count
    characters_per_token: Count
    header_overhead_characters: Count
    min_similarity: Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]
    max_words: Count
    max_question_characters: Count
    rerank_enabled: bool
    rerank_page: Count
    rerank_query_characters: Count
    rerank_keep_within: Annotated[float, Field(ge=0, allow_inf_nan=False)]


def load_retrieval(path: Path | None = None) -> RetrievalSettings:
    """Load a complete explicit policy, without private layer merging."""
    try:
        text = (
            path.read_text(encoding="utf-8")
            if path is not None
            else files("anamnema").joinpath("config/retrieval.json").read_text(encoding="utf-8")
        )
        return RetrievalSettings.model_validate_json(text)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError(f"invalid retrieval configuration ({type(exc).__name__})") from exc
