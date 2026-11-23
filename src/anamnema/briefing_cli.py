"""Explicit briefing requests keep private evidence out of argv and delegate the shared core."""

import argparse
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from anamnema.briefing_builder import BriefBuilder
from anamnema.briefing_files import BriefFiles, brief_file_policy
from anamnema.briefing_policy import briefing_policy
from anamnema.briefing_select import BriefSelector, brief_selector
from anamnema.continuity_state import source_text
from anamnema.graph_store import GraphStore
from anamnema.judge_config import JudgeConfig, judge_policy
from anamnema.memory_sources import SourceLookup
from anamnema.native_judge import NativeJudge
from anamnema.setup_config import Nonempty, SetupError, StrictModel
from anamnema.speech_policy import speech_policy


class BriefRequest(StrictModel):
    subject: Nonempty
    selectors: tuple[BriefSelector, ...]


def add_brief_arguments(parser: argparse.ArgumentParser) -> None:
    """The request file carries the subject/selectors, not private note text in argv."""
    selected = parser.add_mutually_exclusive_group(required=True)
    selected.add_argument("--request", type=Path)
    selected.add_argument("--rebuild", help="prior bundle identity to refresh the same focus")
    parser.add_argument("--judge-config", type=Path)
    parser.add_argument("--judge-policy", type=Path)
    parser.add_argument("--briefing-config", type=Path)
    parser.add_argument("--briefing-root", type=Path, required=True)
    parser.add_argument("--briefing-files-config", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")


def brief_request(args: argparse.Namespace) -> BriefRequest:
    """Bounded no-infrastructure parsing is shared by preview and generation."""
    try:
        limit = briefing_policy(args.briefing_config).read_bytes
        if args.rebuild:
            manifest, _ = BriefFiles(
                args.briefing_root, brief_file_policy(args.briefing_files_config)
            ).read(args.instance, args.rebuild)
            keys = manifest.metadata.get("selectors")
            if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
                raise SetupError("briefing has no valid selector provenance for rebuild")
            request = BriefRequest.model_validate(
                {
                    "subject": manifest.metadata.get("subject"),
                    "selectors": tuple(brief_selector(key) for key in keys if isinstance(key, str)),
                }
            )
        else:
            request = BriefRequest.model_validate_json(source_text(args.request, limit))
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("briefing request cannot be read or validated") from exc
    if not request.subject.strip() or not request.selectors:
        raise SetupError("briefing request needs a subject and selectors")
    BriefFiles(args.briefing_root, brief_file_policy(args.briefing_files_config))
    return request


def run_brief(args: argparse.Namespace, graph: GraphStore, sources: SourceLookup) -> dict[str, Any]:
    """No configured/default native runner is discovered from the user's existing session."""
    request = brief_request(args)
    if args.judge_config is None:
        raise SetupError("brief --apply requires --judge-config")
    policy = judge_policy(args.judge_policy)
    try:
        deployment = JudgeConfig.model_validate_json(
            source_text(args.judge_config, policy.max_input_bytes)
        )
    except (OSError, UnicodeError, ValidationError) as exc:
        raise SetupError("native judge configuration cannot be read or validated") from exc
    builder = BriefBuilder(
        graph,
        sources,
        NativeJudge(deployment, policy),
        briefing_policy(args.briefing_config),
        speech_policy(args.speech_config),
        BriefFiles(args.briefing_root, brief_file_policy(args.briefing_files_config)),
    )
    return (
        builder.rebuild(args.instance, args.rebuild)
        if args.rebuild
        else builder.build(args.instance, request.subject, request.selectors)
    )
