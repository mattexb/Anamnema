"""Explicit native installation and offline journal status; planning is the default."""

import argparse
from pathlib import Path
from typing import Any

from anamnema.batch_journal import BatchJournal
from anamnema.batch_models import batch_policy
from anamnema.install_profile import install_profile
from anamnema.installation import NativeInstaller
from anamnema.setup_config import SetupError


def add_install(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", type=Path, help="explicit nonsecret installation JSON")
    parser.add_argument(
        "--allow-downloads",
        action="store_true",
        help="consent to the listed source/model downloads",
    )
    parser.add_argument(
        "--through",
        choices=(
            "platform",
            "prerequisites",
            "databases",
            "models",
            "schema",
            "services",
            "hooks",
            "schedule",
            "verify",
        ),
        default="verify",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument(
        "--status", metavar="JOB", help="offline journal status, not a live health claim"
    )
    parser.add_argument(
        "--state-root", type=Path, help="installation state root for offline status"
    )
    parser.add_argument("--instance", help="owning assistant for offline status")


def run_install(args: argparse.Namespace) -> dict[str, Any]:
    if not args.apply and (args.allow_downloads or args.through != "verify"):
        raise SetupError("--allow-downloads and --through require explicit --apply")
    if args.status is not None:
        if args.profile is not None or args.state_root is None or args.instance is None:
            raise SetupError(
                "install --status requires --state-root and --instance, without --profile"
            )
        journal = BatchJournal.open_existing(
            args.state_root / "jobs", args.instance, args.status, batch_policy()
        )
        state = journal.inspect()
        return {
            "mode": "status",
            "job": args.status,
            "journal_complete": state.complete,
            "stages": [stage.model_dump(mode="json") for stage in state.stages],
            "live_health_checked": False,
            "release_ready": False,
        }
    if args.profile is None or not args.profile.is_absolute():
        raise SetupError("install requires an explicit absolute --profile")
    if args.state_root is not None or args.instance is not None:
        raise SetupError("install owner and state come from its profile, not status overrides")
    installer = NativeInstaller(install_profile(args.profile), args.profile)
    if not args.apply:
        return installer.plan()
    return installer.apply(allow_downloads=args.allow_downloads, through=args.through)
