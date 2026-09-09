"""Unit tests for sandbox resource limits (repopilot/sandbox/limits.py)."""

from __future__ import annotations

import pytest

from repopilot.sandbox.limits import DEFAULT_LIMITS, SandboxLimits


def test_defaults_are_network_off_and_bounded() -> None:
    assert DEFAULT_LIMITS.network == "none"
    assert DEFAULT_LIMITS.timeout_seconds == 600
    assert DEFAULT_LIMITS.cpus > 0
    assert DEFAULT_LIMITS.pids_limit > 0


def test_docker_run_args_cover_every_limit() -> None:
    args = SandboxLimits(cpus=2.0, memory="4g", pids_limit=128).docker_run_args()
    expected = ["--cpus", "2.0", "--memory", "4g", "--pids-limit", "128", "--network", "none"]
    assert args[: len(expected)] == expected
    assert "--cap-drop" in args
    assert "no-new-privileges" in args


@pytest.mark.parametrize(
    "bad", [{"cpus": 0}, {"pids_limit": -1}, {"timeout_seconds": 0}, {"workspace": "ws"}]
)
def test_invalid_limits_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SandboxLimits(**bad)  # type: ignore[arg-type]


def test_limits_are_immutable() -> None:
    with pytest.raises(AttributeError):
        DEFAULT_LIMITS.cpus = 4.0  # type: ignore[misc]
