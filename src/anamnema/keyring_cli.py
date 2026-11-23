"""Explicit secret-safe manual keyring commands; never print values or accept them as argv."""

import argparse
import getpass
import os
import re
import warnings
from pathlib import Path
from typing import Any, TextIO

from anamnema.ingest_config import archive_settings
from anamnema.secret_service import SecretService
from anamnema.setup_config import SetupError, load_config


def add_keyring(parser: argparse.ArgumentParser) -> None:
    """Lookup and replacement are explicit apply operations; default previews read no secret."""
    actions = parser.add_subparsers(dest="keyring_action", required=True)
    for name in ("verify", "put", "run"):
        command = actions.add_parser(name)
        command.add_argument("--config", type=Path)
        command.add_argument(
            "--archive-config", type=Path, help="shared keyring bus/timeout profile"
        )
        if name != "run":
            command.add_argument("--entry", required=True, help="nonsecret entry name")
            command.add_argument("--field", required=True, help="nonsecret field name")
        else:
            command.add_argument(
                "--secret", action="append", help="ENV=ENTRY:FIELD reference, never a value"
            )
            command.add_argument(
                "program", nargs=argparse.REMAINDER, help="-- /absolute/program arguments"
            )
        mode = command.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--dry-run", action="store_true")
        if name == "put":
            command.add_argument(
                "--confirm-replace",
                action="store_true",
                help="allow this exact item to be created or replaced",
            )
            command.add_argument(
                "--stdin",
                action="store_true",
                help="read exact UTF-8 value through EOF; otherwise use a hidden terminal prompt",
            )


def run_keyring(args: argparse.Namespace, stream: TextIO) -> dict[str, Any]:
    """Return status only; on failed verification state may have changed but no value is exposed."""
    if not args.apply:
        return {"mode": "dry-run", "operation": args.keyring_action, "keyring_accessed": False}
    if args.keyring_action == "put" and not args.confirm_replace:
        raise SetupError("keyring put requires --confirm-replace; no secret read or stored")
    setup = load_config(args.config)
    service = SecretService(setup.keyring, archive_settings(args.archive_config))
    if args.keyring_action == "run":
        _run_program(args, service)
        raise SetupError("keyring child unexpectedly returned without exec")
    service.attributes(args.entry, args.field)
    if args.keyring_action == "verify":
        service.lookup(args.entry, args.field)
        return {"mode": "apply", "available": True, "value_printed": False}
    if args.stdin:
        try:
            value = stream.read(service.policy.max_value_bytes + 1)
        except UnicodeError as exc:
            raise SetupError("keyring input is not valid UTF-8") from exc
    else:
        if not stream.isatty():
            raise SetupError("keyring put needs a terminal or explicit --stdin; no echo fallback")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                value = getpass.getpass("Secret value (hidden): ", stream=None)
        except (EOFError, getpass.GetPassWarning) as exc:
            raise SetupError("hidden keyring input unavailable; no echo fallback") from exc
    service.store(args.entry, args.field, value)
    return {"mode": "apply", "stored": True, "verified": True, "value_printed": False}


def _run_program(args: argparse.Namespace, service: SecretService) -> None:
    """Replace the user wrapper with an explicit program; only its environment receives values."""
    if len(args.program) < 2 or args.program[0] != "--":
        raise SetupError("keyring run requires -- followed by an absolute program")
    program = args.program[1:]
    if not Path(program[0]).is_absolute() or not Path(program[0]).is_file():
        raise SetupError("keyring run requires an available absolute executable")
    references: list[str] = args.secret or []
    if not 1 <= len(references) <= service.policy.max_injections:
        raise SetupError("keyring run secret reference count exceeds configured bounds")
    bindings: dict[str, tuple[str, str]] = {}
    for reference in references:
        variable, separator, item = reference.partition("=")
        entry, field_separator, field = item.rpartition(":")
        if (
            not separator
            or not field_separator
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable) is None
            or variable in bindings
        ):
            raise SetupError("keyring run needs unique ENV=ENTRY:FIELD references")
        service.attributes(entry, field)
        bindings[variable] = entry, field
    environment = dict(os.environ)
    for variable, (entry, field) in bindings.items():
        value = service.lookup(entry, field)
        if "\x00" in value:
            raise SetupError("keyring value cannot be placed in a process environment")
        environment[variable] = value
    try:
        os.execve(program[0], program, environment)  # noqa: S606 - explicit absolute program; no shell
    except (OSError, ValueError, UnicodeError) as exc:
        raise SetupError(
            f"keyring child cannot start ({type(exc).__name__}); values withheld"
        ) from exc
