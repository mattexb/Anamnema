"""Strict entity/card operations shared by CLI and authenticated HTTP, with one owner boundary."""

from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from anamnema.card_builder import CardBuilder
from anamnema.card_graph import CardGraph
from anamnema.card_moves import CardMoves
from anamnema.setup_config import Nonempty, SetupError, StrictModel


class EntityRequest(StrictModel):
    """No model-generated identity can silently become an explicit confirmed entity."""

    operation: Literal["status", "create", "confirm", "drop", "undrop", "link", "pin", "unpin"]
    name: Nonempty
    reason: Nonempty | None = None
    kind: Nonempty | None = None
    note: Nonempty | None = None
    pinned: bool | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Reject incomplete or unrelated arguments rather than guessing a destructive action."""
        if self.operation != "status" and self.reason is None:
            raise ValueError("entity mutation needs a reason")
        if (self.operation == "create") != (self.kind is not None):
            raise ValueError("only create requires entity kind")
        if (self.operation in ("link", "pin", "unpin")) != (self.note is not None):
            raise ValueError("only link, pin or unpin requires a note")
        if self.pinned is not None and self.operation != "link":
            raise ValueError("only a link accepts a pin flag")
        return self


class CardRequest(StrictModel):
    """Reading does not implicitly invoke generation; rebuild is an explicit operation."""

    name: Nonempty
    page: Annotated[int, Field(gt=0)] = 1
    rebuild: bool = False
    operation: Literal["read", "promote", "demote", "forget"] = "read"
    statement: Nonempty | None = None
    reason: Nonempty | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        """Manual decisions cannot accidentally invoke generation or pagination."""
        if self.operation == "read":
            if self.statement is not None or self.reason is not None:
                raise ValueError("card read does not accept statement or reason")
        elif self.statement is None or self.reason is None or self.rebuild or self.page != 1:
            raise ValueError("card move needs statement/reason without rebuild or pagination")
        return self


def entity_operation(graph: CardGraph, instance: str, request: EntityRequest) -> dict[str, Any]:
    """Accountable manual lifecycle operations, including retirement and restoration history."""
    reason = request.reason or ""
    if request.operation == "create":
        graph.create(instance, request.name, request.kind or "", reason)
    elif request.operation in ("confirm", "drop", "undrop"):
        graph.decide(instance, request.name, request.operation, reason)
    elif request.operation == "link":
        graph.link(instance, request.name, request.note or "", reason, request.pinned)
    elif request.operation in ("pin", "unpin"):
        graph.pin(instance, request.name, request.note or "", reason, request.operation == "pin")
    result: dict[str, Any] = {
        "entity": graph.entity(instance, request.name),
        "history": graph.history(instance, request.name),
    }
    if request.operation in ("pin", "unpin"):
        result.update(note=request.note, pinned=request.operation == "pin")
    return result


def card_operation(
    graph: CardGraph, builder: CardBuilder | None, instance: str, request: CardRequest
) -> dict[str, Any]:
    """Both service and CLI refuse stale output and missing generation credentials explicitly."""
    rebuilt = None
    if request.operation != "read":
        moves = CardMoves(graph)
        if request.operation == "forget":
            return moves.forget(
                instance, request.name, request.statement or "", request.reason or ""
            )
        return moves.move(
            instance,
            request.name,
            request.statement or "",
            request.reason or "",
            request.operation == "promote",
        )
    if request.rebuild:
        if builder is None:
            raise SetupError("card rebuild requires an explicitly selected card model")
        rebuilt = builder.rebuild(instance, request.name)
    result = graph.card(instance, request.name, request.page)
    if rebuilt:
        result["rebuild"] = rebuilt
    return result
