"""Inspect immutable bundles or explicitly change a session's briefing/read stack."""

import argparse
from pathlib import Path
from typing import Any

from anamnema.briefing_files import BriefFiles, brief_file_policy
from anamnema.briefing_policy import briefing_policy
from anamnema.briefing_stack import BriefStack, stack_policy
from anamnema.continuity import Continuity
from anamnema.continuity_config import continuity_config, continuity_policy
from anamnema.setup_config import SetupError


def add_briefing_delivery(parser: argparse.ArgumentParser) -> None:
    """Read/status are read-only; deliver/drop require explicit apply for all changes."""
    actions = parser.add_subparsers(dest="briefing_action", required=True)
    for name in ("read", "status", "deliver", "drop"):
        command = actions.add_parser(name)
        command.add_argument("--instance", required=True)
        command.add_argument("--briefing-root", type=Path, required=True)
        command.add_argument("--briefing-files-config", type=Path)
        if name != "status":
            command.add_argument(
                "--briefing", required=True, help="content-addressed bundle identity"
            )
            command.add_argument("--page", type=int, default=1)
        if name != "read":
            command.add_argument("--continuity-config", type=Path, required=True)
            command.add_argument("--continuity-policy", type=Path)
            command.add_argument("--briefing-config", type=Path)
            command.add_argument("--stack-config", type=Path)
            command.add_argument("--stack-root", type=Path, required=True)
            command.add_argument("--session", required=True)
        if name in {"deliver", "drop"}:
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--apply", action="store_true")
            mode.add_argument("--dry-run", action="store_true")


def run_briefing_delivery(args: argparse.Namespace) -> dict[str, Any]:
    """Never open an archive, model, keyring or network for an already committed bundle."""
    bundles = BriefFiles(args.briefing_root, brief_file_policy(args.briefing_files_config))
    if args.briefing_action == "read":
        manifest, pages = bundles.read(args.instance, args.briefing)
        page: int = args.page
        if not 1 <= page <= len(pages):
            raise SetupError("briefing page is outside the committed bundle")
        return {
            "briefing": args.briefing,
            "page": args.page,
            "file": str(pages[page - 1]),
            "metadata": manifest.metadata,
            "required_read_registered": False,
        }
    config = continuity_config(args.continuity_config)
    if config.instance != args.instance:
        raise SetupError("briefing assistant differs from the continuity deployment")
    core = Continuity(config, continuity_policy(args.continuity_policy))
    stack = BriefStack(
        args.stack_root,
        bundles,
        core,
        briefing_policy(args.briefing_config),
        stack_policy(args.stack_config),
    )
    if args.briefing_action == "status":
        return stack.status(args.session)
    if not args.apply:
        bundles.read(args.instance, args.briefing)
        return {
            "mode": "dry-run",
            "operation": args.briefing_action,
            "stack": stack.status(args.session),
            "required_read_registered": False,
            "files_written": False,
        }
    if args.briefing_action == "deliver":
        return stack.deliver(args.session, args.briefing, args.page)
    return stack.drop(args.session, args.briefing, args.page)
