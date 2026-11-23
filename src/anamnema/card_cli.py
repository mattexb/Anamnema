"""Explicit card generation and entity decisions; no archive/model dependency for inspection."""

import argparse
from typing import Any

from pydantic import ValidationError

from anamnema.card_builder import CardBuilder
from anamnema.card_graph import CardGraph
from anamnema.card_operations import CardRequest, EntityRequest, card_operation, entity_operation
from anamnema.card_policy import CardPolicy, card_policy
from anamnema.card_writer import CardWriter
from anamnema.extraction_config import extraction_config
from anamnema.graph_store import GraphStore
from anamnema.ingest_config import ArchiveSettings
from anamnema.local_model import local_model
from anamnema.setup_config import SetupConfig, SetupError

CARD_COMMANDS = ("entities", "card", "build-cards")


def add_card_arguments(name: str, parser: argparse.ArgumentParser) -> None:
    """Preserve reasoned create/drop/undrop while keeping reads separate from model calls."""
    if name == "entities":
        operations = parser.add_mutually_exclusive_group(required=True)
        for operation in ("create", "confirm", "drop", "undrop", "show"):
            operations.add_argument(f"--{operation}", metavar="NAME")
        operations.add_argument("--link", metavar="NOTE")
        operations.add_argument("--pin-note", metavar="NOTE")
        operations.add_argument("--unpin-note", metavar="NOTE")
        parser.add_argument("--entity", help="entity name for a settled note link")
        parser.add_argument("--type", dest="entity_type")
        parser.add_argument("--reason")
        parser.add_argument("--pin", action="store_true", help="pin an explicitly linked note")
    else:
        parser.add_argument("--entity", required=True)
        parser.add_argument("--page", type=int, default=1)
        if name == "card":
            moves = parser.add_mutually_exclusive_group()
            moves.add_argument("--promote", metavar="STATEMENT")
            moves.add_argument("--demote", metavar="STATEMENT")
            moves.add_argument("--forget-move", metavar="MOVE_ID")
            parser.add_argument("--reason")


def selected_builder(
    args: argparse.Namespace,
    setup: SetupConfig,
    archive: ArchiveSettings,
    store: GraphStore,
    policy: CardPolicy | None = None,
) -> CardBuilder | None:
    """Only an explicitly supplied card model file permits a generation call."""
    model = getattr(args, "card_model_file", None)
    if model is None:
        return None
    policy = policy or card_policy(args.cards_config)
    provider = local_model(setup, archive, extraction_config(args.extraction_config), model)
    return CardBuilder(CardGraph(store, policy), CardWriter(provider, policy))


def run_cards(
    args: argparse.Namespace, setup: SetupConfig, archive: ArchiveSettings, store: GraphStore
) -> dict[str, Any]:
    """Run the same typed owner-scoped operations as the HTTP service."""
    policy = card_policy(args.cards_config)
    graph = CardGraph(store, policy)
    if args.command == "entities":
        operation, name = _entity_choice(args)
        if operation != "status" and not args.reason:
            raise SetupError("entity mutation requires --reason")
        if operation == "create" and not args.entity_type:
            raise SetupError("entity creation requires --type")
        try:
            request = EntityRequest(
                operation=operation,
                name=name,
                kind=args.entity_type,
                reason=args.reason,
                note=args.link or args.pin_note or args.unpin_note,
                pinned=True if args.pin else None,
            )
        except ValidationError as exc:
            raise SetupError(
                "entity operation has incompatible fields; see entities --help"
            ) from exc
        if request.operation != "status":
            store.ensure_schema()
        return entity_operation(graph, args.instance, request)
    builder = selected_builder(args, setup, archive, store, policy)
    if args.page < 1:
        raise SetupError("card page must be positive")
    operation = "read"
    statement = None
    for action, flag in (("promote", "promote"), ("demote", "demote"), ("forget", "forget_move")):
        if getattr(args, flag, None) is not None:
            operation = action
            statement = getattr(args, flag)
    try:
        request = CardRequest.model_validate(
            {
                "name": args.entity,
                "page": args.page,
                "rebuild": args.command == "build-cards",
                "operation": operation,
                "statement": statement,
                "reason": getattr(args, "reason", None),
            }
        )
    except ValidationError as exc:
        raise SetupError("card move needs a reason and cannot use pagination/rebuild") from exc
    return card_operation(graph, builder, args.instance, request)


def _entity_choice(args: argparse.Namespace) -> tuple[Any, str]:
    for operation in ("create", "confirm", "drop", "undrop", "show"):
        name = getattr(args, operation)
        if name is not None:
            return ("status" if operation == "show" else operation), name
    if args.link is not None and args.entity:
        return "link", args.entity
    for operation in ("pin", "unpin"):
        if getattr(args, operation + "_note") is not None and args.entity:
            return operation, args.entity
    raise SetupError("an entity link or pin operation requires --entity NAME")
