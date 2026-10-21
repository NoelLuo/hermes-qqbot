# OpenID-looking constants below are synthetic parser fixtures, not real QQ identities.
"""QQ OpenIDs resolve without directory history; names keep directory semantics."""
from unittest.mock import Mock

import pytest

from hermes_cli.plugins import discover_plugins
from tools.send_message_targets import _parse_target_ref, resolve_send_target


@pytest.mark.parametrize("target", [
    "ABCDEF0123456789ABCDEF0123456789",
    "abcdef0123456789aBcDeF0123456789",
])
def test_native_openid_resolves_without_channel_directory(monkeypatch, target):
    discover_plugins()
    directory = Mock(side_effect=AssertionError("native OpenID must not need directory history"))
    monkeypatch.setattr("gateway.channel_directory.resolve_channel_name", directory)
    assert _parse_target_ref("qqbot", " " + target + " ") == (target, None, True)
    assert resolve_send_target("qqbot", target) == (target, None, None)
    directory.assert_not_called()


@pytest.mark.parametrize("reference", [
    "study-group", "", "A" * 31, "A" * 33, "G" * 32,
])
def test_non_openid_keeps_directory_resolution(monkeypatch, reference):
    discover_plugins()
    target = "0123456789ABCDEF0123456789ABCDEF"
    directory = Mock(return_value=target)
    monkeypatch.setattr("gateway.channel_directory.resolve_channel_name", directory)
    assert _parse_target_ref("qqbot", reference) == (None, None, False)
    assert resolve_send_target("qqbot", reference) == (target, None, None)
    directory.assert_called_once_with("qqbot", reference)
    # Guild/channel IDs retain the existing generic numeric target path.
    assert _parse_target_ref("qqbot", "123456789") == ("123456789", None, True)
