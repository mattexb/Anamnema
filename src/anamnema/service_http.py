"""Shared authenticated, bounded JSON routes for memory and CPU inference services."""

import asyncio
import hmac
import logging
import time
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import JsonValue, TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool

from anamnema.archive_models import canonical
from anamnema.graph_store import MemoryConflict, MemoryDenied
from anamnema.memory_models import MemoryInputError
from anamnema.service_config import ServiceSettings
from anamnema.setup_config import SetupError, StrictModel

LOG = logging.getLogger(__name__)
REPLY = TypeAdapter(dict[str, JsonValue])


class JsonService:
    """Token-derived ownership and typed JSON without permissive Python coercion."""

    def __init__(self, settings: ServiceSettings, tokens: dict[str, str], title: str) -> None:
        if (
            not tokens
            or len(set(tokens.values())) != len(tokens)
            or any(
                not owner or not token or any(not 33 <= ord(c) <= 126 for c in token)
                for owner, token in tokens.items()
            )
        ):
            raise SetupError("service requires distinct nonempty instance bearer credentials")
        self.settings, self.tokens = settings, tokens
        self.app = FastAPI(
            title=title, version="1", docs_url=None, redoc_url=None, openapi_url=None
        )
        self.gate = asyncio.Semaphore(settings.max_inflight)
        self.rates: dict[str, tuple[float, int]] = {}
        self.schemas: dict[str, Any] = {}
        bearer = HTTPBearer(auto_error=False)

        async def principal(
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ) -> str:
            given = credentials.credentials if credentials else ""
            owner = None
            for instance, expected in self.tokens.items():
                if hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
                    owner = instance
            if owner is None:
                LOG.warning("event=authentication_failed")
                raise HTTPException(
                    401, "invalid service bearer", headers={"WWW-Authenticate": "Bearer"}
                )
            now = time.monotonic()
            began, count = self.rates.get(owner, (now, 0))
            if now - began >= settings.rate_period_seconds:
                began, count = now, 0
            if count >= settings.requests_per_period:
                LOG.warning("event=rate_limited")
                raise HTTPException(
                    429,
                    "service request rate exceeded",
                    headers={"Retry-After": str(int(settings.rate_period_seconds) + 1)},
                )
            self.rates[owner] = began, count + 1
            return owner

        self.principal = principal

        @self.app.get(settings.health_path)
        async def health(instance: Annotated[str, Depends(principal)]) -> dict[str, str]:
            return {"status": "ok"}

        @self.app.get(settings.openapi_path, include_in_schema=False)
        async def specification(instance: Annotated[str, Depends(principal)]) -> dict[str, Any]:
            schema = self.app.openapi()
            schema.setdefault("components", {}).setdefault("schemas", {}).update(self.schemas)
            return schema

    def post[T: StrictModel](
        self,
        path: str,
        model: type[T],
        operation: Callable[[str, T], dict[str, Any]],
        description: str,
    ) -> None:
        """Register one typed operation with bounded streaming and sanitized errors."""
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        self.schemas.update(schema.pop("$defs", {}))
        self.schemas[model.__name__] = schema
        principal = self.principal

        async def endpoint(
            request: Request, instance: Annotated[str, Depends(principal)]
        ) -> JSONResponse:
            try:
                raw = bytearray()
                async with asyncio.timeout(self.settings.body_timeout_seconds):
                    async for part in request.stream():
                        raw.extend(part)
                        if len(raw) > self.settings.max_request_bytes:
                            raise HTTPException(413, "request body exceeds configured limit")
                value = model.model_validate_json(bytes(raw))
                async with self.gate:
                    answer = await run_in_threadpool(operation, instance, value)
                try:
                    answer = REPLY.validate_python(answer, strict=True)
                except ValidationError as exc:
                    raise SetupError("service produced an invalid response") from exc
                if len(canonical(answer)) > self.settings.max_response_bytes:
                    raise HTTPException(413, "response exceeds configured limit")
                return JSONResponse(answer, headers={"Cache-Control": "no-store"})
            except ValidationError:
                LOG.warning("event=request_validation_failed")
                raise HTTPException(422, "request violates the documented schema") from None
            except TimeoutError:
                raise HTTPException(408, "request body timed out") from None
            except MemoryDenied:
                LOG.warning("event=ownership_denied")
                raise HTTPException(
                    403, "memory is absent or belongs to another assistant"
                ) from None
            except MemoryConflict:
                raise HTTPException(409, "stable identity conflicts with stored content") from None
            except MemoryInputError as exc:
                raise HTTPException(422, str(exc)) from None
            except SetupError as exc:
                raise HTTPException(503, str(exc)) from None

        self.app.add_api_route(
            path,
            endpoint,
            methods=["POST"],
            name=model.__name__,
            description=description,
            openapi_extra={
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": f"#/components/schemas/{model.__name__}"}
                        }
                    },
                }
            },
            responses={
                200: {
                    "description": "Operation result",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                401: {"description": "Invalid bearer"},
                403: {"description": "Ownership denied"},
                409: {"description": "Identity conflict"},
                413: {"description": "Body limit"},
                422: {"description": "Invalid typed input"},
                429: {"description": "Rate limit"},
                503: {"description": "Dependency unavailable"},
            },
        )
