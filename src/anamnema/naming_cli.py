"""Explicit naming command over the common graph, source and model workflow."""

import argparse
from pathlib import Path
from typing import Any

from anamnema.extraction_config import extraction_config
from anamnema.graph_store import GraphStore
from anamnema.ingest_config import ArchiveSettings
from anamnema.local_model import local_model
from anamnema.memory_sources import SourceLookup
from anamnema.naming_builder import NamingBuilder
from anamnema.naming_policy import naming_policy
from anamnema.naming_writer import NamingWriter
from anamnema.search_services import CPUEncoder
from anamnema.setup_config import SetupConfig, SetupError


def add_naming_arguments(parser: argparse.ArgumentParser) -> None:
    """No implicit all-store model run: select explicit notes or a bounded pending batch."""
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument("--note", action="append", help="owned note id; repeat as needed")
    selected.add_argument("--pending", action="store_true", help="one bounded pending batch")
    parser.add_argument("--model-file", type=Path, help="explicit local model file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")


def run_naming(
    args: argparse.Namespace,
    setup: SetupConfig,
    archive: ArchiveSettings,
    graph: GraphStore,
    encoder: CPUEncoder,
    sources: SourceLookup,
) -> dict[str, Any]:
    """Only apply reaches here; use the exact same guarded builder as local batch jobs."""
    if args.model_file is None:
        raise SetupError("naming apply requires --model-file")
    policy = naming_policy(args.naming_config)
    provider = local_model(
        setup, archive, extraction_config(args.extraction_config), args.model_file
    )
    builder = NamingBuilder(graph, NamingWriter(provider, policy), encoder, sources)
    graph.ensure_schema()
    seeded = builder.graph.seed(args.instance)
    selected = tuple(
        args.note or [note["id"] for note in builder.graph.pending(args.instance, policy.batch)]
    )
    return {**builder.build(args.instance, selected, apply=True), "seeded": seeded}
