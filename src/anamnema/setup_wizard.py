"""Collect nonsecret choices only; setup execution stays separate and unavailable."""

from __future__ import annotations

import getpass
import json
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from pydantic import ValidationError

from anamnema.setup_choices import ServiceAccounts
from anamnema.setup_config import Paths, SetupConfig, SetupError, parse_config


class WizardCancelled(SetupError):
    """Input ended or the operator interrupted before any answer file was saved."""


class Questions:
    """Read answers while keeping prompts out of machine-readable stdout."""

    def __init__(self, source: TextIO, output: TextIO) -> None:
        self.source = source
        self.output = output

    def text(self, label: str, default: str = "") -> str:
        """Blank input accepts a visible default; EOF is cancellation, not yes."""
        suffix = f" [{default}]" if default else ""
        print(f"{label}{suffix}: ", end="", file=self.output, flush=True)
        line = self.source.readline()
        if not line:
            raise WizardCancelled("setup cancelled: input ended; no answers saved")
        return line.strip() or default

    def choice(self, label: str, options: tuple[str, ...], default: str) -> str:
        """Retry a bounded option set without reflecting unrecognized input."""
        while True:
            answer = self.text(f"{label} ({'/'.join(options)})", default).lower()
            if answer in options:
                return answer
            print("Choose one of the listed options.", file=self.output)

    def yes(self, label: str, default: bool) -> bool:
        """Keep yes/no choices and confirmation explicit."""
        return self.choice(label, ("yes", "no"), "yes" if default else "no") == "yes"

    def account(self, label: str, default: str, *, optional: bool = False) -> str | None:
        """Validate account syntax early, without printing a rejected value."""
        while True:
            answer = self.text(label, default)
            if optional and not answer:
                return None
            try:
                ServiceAccounts(application=answer)
            except ValidationError:
                print("Use a non-root Linux account name, not a password.", file=self.output)
            else:
                return answer


def collect(
    config: SetupConfig, questions: Questions, current_user: Callable[[], str] = getpass.getuser
) -> SetupConfig:
    """Return a validated candidate without saving or changing any service."""
    print(
        "Anamnema setup: Debian 13, native services. No passwords are requested.\n"
        "This wizard plans only; it does not install services or unlock the keyring.",
        file=questions.output,
    )
    previous = config.choices
    logged_out = questions.yes(
        "Should processing continue while nobody is logged in?",
        previous.run_when_logged_out or False,
    )
    print(
        "Application user services run as your account; system services start with the machine.\n"
        "Database/model service accounts remain separately configured.\n"
        "Neither choice automatically unlocks a keyring after reboot.",
        file=questions.output,
    )
    scope = questions.choice(
        "Which application service arrangement do you want to plan?",
        ("user", "system"),
        previous.service_scope or ("system" if logged_out else "user"),
    )
    default_user = previous.accounts.application
    if not default_user:
        try:
            default_user = current_user()
        except OSError:
            print("Cannot detect your account; enter its name below.", file=questions.output)
            default_user = ""
    application = questions.account("Account for the memory application", default_user)
    if scope == "user":
        accounts = {
            "application": application,
            "models": previous.accounts.models or application,
            "archive": previous.accounts.archive,
            "graph": previous.accounts.graph,
        }
    else:
        accounts = {
            "application": application,
            "models": questions.account(
                "Account for model services", previous.accounts.models or application or ""
            ),
            "archive": questions.account(
                "MongoDB account (blank leaves the native package default)",
                previous.accounts.archive or "",
                optional=True,
            ),
            "graph": questions.account(
                "Neo4j account (blank leaves the native package default)",
                previous.accounts.graph or "",
                optional=True,
            ),
        }
    keyring = questions.choice(
        "Keyring setup: follow the manual guide or request guided configuration?",
        ("manual", "guided"),
        previous.keyring_setup or "manual",
    )
    print(
        "The application needs its own account's keyring. Account/unlock compatibility\n"
        "is not verified here. Manual steps: anamnema setup --keyring-guide.\n"
        "Guided apply is not built yet.",
        file=questions.output,
    )
    backend = questions.choice(
        "Where should the notes model run?", ("vulkan", "cpu", "api", "cuda"), config.backend
    )
    paths = config.paths.model_dump()
    if questions.yes("Change the proposed application folders?", False):
        for name, default in paths.items():
            while True:
                value = questions.text(f"Folder for {name}", default)
                try:
                    Paths.model_validate(paths | {name: value})
                except ValidationError:
                    print(
                        "Use an absolute path or ~/path without parent traversal.",
                        file=questions.output,
                    )
                else:
                    paths[name] = value
                    break
    raw = config.model_dump(mode="json")
    raw.update(
        backend=backend,
        paths=paths,
        choices={
            "run_when_logged_out": logged_out,
            "service_scope": scope,
            "accounts": accounts,
            "keyring_setup": keyring,
            "keyring_owner": application,
        },
    )
    return parse_config(json.dumps(raw))


def review(config: SetupConfig, questions: Questions, destination: Path | None) -> bool:
    """Show all choices before asking whether to save to the explicit destination."""
    print("\nProposed nonsecret configuration (not applied):", file=questions.output)
    print(config.model_dump_json(indent=2), file=questions.output)
    print(
        "No services, accounts, keyrings or Claude settings will be changed.", file=questions.output
    )
    return destination is not None and questions.yes("Save these nonsecret answers?", False)
