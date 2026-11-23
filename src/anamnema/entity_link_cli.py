"""``anamnema link-entities``: link due notes to entities with the local model; dry-run default."""

import argparse
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from anamnema.entity_link_graph import EntityLinkGraph, link_pending
from anamnema.entity_link_policy import entity_link_policy
from anamnema.entity_link_writer import EntityLinkWriter
from anamnema.extraction_config import extraction_config
from anamnema.graph_store import connect_graph
from anamnema.ingest_config import archive_settings
from anamnema.local_model import local_model
from anamnema.memory_config import load_memory
from anamnema.setup_config import SetupError, load_config
from anamnema.speech_policy import speech_policy


def add_link_entities(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--instance", required=True)
    for option in (
        "config",
        "archive-config",
        "memory-config",
        "extraction-config",
        "entity-config",
        "speech-config",
    ):
        parser.add_argument(f"--{option}", type=Path)
    parser.add_argument("--model-file", type=Path)
    parser.add_argument("--limit", type=int, default=100)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")


def run_link_entities(args: argparse.Namespace) -> dict[str, Any]:
    """Dry-run counts due notes; apply judges and persists them in policy-sized batches."""
    if args.limit < 1:
        raise SetupError("--limit must be positive")
    policy = entity_link_policy(args.entity_config)
    setup, archive = load_config(args.config), archive_settings(args.archive_config)
    with ExitStack() as resources:
        graph = connect_graph(setup, archive, load_memory(args.memory_config))
        resources.callback(graph.close)
        links = EntityLinkGraph(graph, policy)
        if not args.apply:
            due = links.pending(args.instance, args.limit)
            return {"mode": "dry-run", "due": len(due), "version": policy.version}
        if args.model_file is None:
            raise SetupError("link-entities --apply requires --model-file")
        provider = local_model(
            setup, archive, extraction_config(args.extraction_config), args.model_file
        )
        writer = EntityLinkWriter(provider, policy, speech_policy(args.speech_config))
        return {"mode": "apply", **link_pending(links, writer, args.instance, args.limit)}
