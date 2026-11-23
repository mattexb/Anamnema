"""Settings reconciliation preserves foreign hooks and refuses ownership drift without writes."""

import copy
import shlex

import pytest

from anamnema.hook_merge import HookBinding, merge_hooks
from anamnema.setup_config import SetupError


def binding(**changes):
    return HookBinding(
        **{
            "event": "Stop",
            "command": ("/opt/fixture path/anamnema", "capture-events", "hook", "--apply"),
            "timeout_seconds": 10.0,
            **changes,
        }
    )


def test_first_merge_repeat_upgrade_and_remove_keep_unrelated_settings_exactly():
    foreign = {"matcher": "", "hooks": [{"type": "command", "command": "foreign", "timeout": 3}]}
    settings = {"permissions": {"allow": ["Read"]}, "hooks": {"Stop": [foreign], "Other": []}}
    original = copy.deepcopy(settings)
    initial = merge_hooks(settings, (), (binding(),))
    assert settings == original and initial.changed
    assert initial.settings["permissions"] == original["permissions"]
    assert initial.settings["hooks"]["Stop"] == [foreign, binding().group()]
    repeated = merge_hooks(initial.settings, initial.bindings, initial.bindings)
    assert not repeated.changed and repeated.settings == initial.settings
    upgraded = binding(timeout_seconds=20.0)
    changed = merge_hooks(initial.settings, initial.bindings, (upgraded,))
    assert changed.settings["hooks"]["Stop"] == [foreign, upgraded.group()]
    removed = merge_hooks(changed.settings, changed.bindings, ())
    assert removed.settings == original


def test_argv_shell_metacharacters_are_literals_and_matcher_is_preserved():
    own = binding(
        command=("/fixture/a tool", "$(touch /fixture/not-created)", "it's quoted"),
        matcher="compact",
    )
    group = own.group()
    assert group["matcher"] == "compact"
    assert shlex.split(group["hooks"][0]["command"]) == list(own.command)


@pytest.mark.parametrize("change", ["missing", "edited", "duplicate"])
def test_user_modified_owned_group_is_never_removed_or_overwritten(change):
    own = binding()
    group = own.group()
    groups = [] if change == "missing" else [group]
    if change == "edited":
        group["matcher"] = "user-added-match"
    if change == "duplicate":
        groups.append(copy.deepcopy(group))
    settings = {"hooks": {"Stop": groups}, "custom": True}
    before = copy.deepcopy(settings)
    with pytest.raises(SetupError, match="owned Claude hook"):
        merge_hooks(settings, (own,), ())
    assert settings == before


def test_existing_foreign_duplicate_is_not_adopted_and_disabled_hooks_stay_disabled():
    own = binding()
    settings = {"hooks": {"Stop": [own.group()]}}
    with pytest.raises(SetupError, match="outside the ownership"):
        merge_hooks(settings, (), (own,))
    with pytest.raises(SetupError, match="explicitly disabled"):
        merge_hooks({"disableAllHooks": True}, (), (own,))
    with pytest.raises(SetupError, match="bindings repeat"):
        merge_hooks({}, (), (own, own))


@pytest.mark.parametrize(
    "settings", [{"hooks": []}, {"hooks": {"Stop": {}}}, {"hooks": {"Stop": ["bad"]}}]
)
def test_malformed_event_refuses_without_mutating_input(settings):
    before = copy.deepcopy(settings)
    with pytest.raises(SetupError):
        merge_hooks(settings, (), (binding(),))
    assert settings == before
