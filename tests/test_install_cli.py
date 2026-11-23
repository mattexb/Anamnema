"""The real parser rejects contradictory installer modes before profile or keyring access."""

import pytest

from anamnema.cli import _parser
from anamnema.install_cli import run_install
from anamnema.setup_config import SetupError


@pytest.mark.parametrize(
    "options",
    [
        ["--allow-downloads"],
        ["--dry-run", "--through", "services"],
        ["--status", "a" * 64, "--profile", "/nonexistent.json"],
        ["--apply", "--profile", "relative.json"],
    ],
)
def test_invalid_mode_never_opens_the_profile(options):
    args = _parser().parse_args(["install", *options])
    with pytest.raises(SetupError):
        run_install(args)


def test_offline_status_rejects_path_traversal_without_any_native_command(tmp_path):
    args = _parser().parse_args(
        ["install", "--status", "../other", "--state-root", str(tmp_path), "--instance", "fixture"]
    )
    with pytest.raises(SetupError, match="invalid batch status"):
        run_install(args)
