"""Shared test configuration.

* ``pytester`` is enabled so the in-container pytest report plugin can be
  exercised in a real (subprocess) pytest session.
* Tests marked ``@pytest.mark.docker`` need a Docker daemon and are skipped
  automatically when none is reachable (or when REPOPILOT_SKIP_DOCKER is set).
* Tests marked ``@pytest.mark.llm("<provider>")`` make one real, paid model
  call.  They run only when that provider's key is in the environment (a
  ``.env`` next to ``pyproject.toml`` is loaded first) and never in CI, which
  has no keys.  Run them on purpose with ``uv run pytest -m llm``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repopilot.models.config import KEY_VARS, Provider, has_api_key, load_env
from repopilot.sandbox.docker import docker_status

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    _skip_docker_tests(items)
    _skip_llm_tests(items)


def _skip_docker_tests(items: list[pytest.Item]) -> None:
    docker_items = [item for item in items if "docker" in item.keywords]
    if not docker_items:
        return
    if os.environ.get("REPOPILOT_SKIP_DOCKER"):
        reason = "REPOPILOT_SKIP_DOCKER is set"
    else:
        available, detail = docker_status()
        if available:
            return
        reason = f"{detail} -- start Docker Desktop / OrbStack, then rerun with -m docker"
    skip = pytest.mark.skip(reason=reason)
    for item in docker_items:
        item.add_marker(skip)


def _skip_llm_tests(items: list[pytest.Item]) -> None:
    llm_items = [item for item in items if "llm" in item.keywords]
    if not llm_items:
        return
    load_env(REPO_ROOT / ".env")
    for item in llm_items:
        marker = item.get_closest_marker("llm")
        if marker is None or not marker.args:
            item.add_marker(pytest.mark.skip(reason="llm marker needs a provider argument"))
            continue
        provider = Provider(marker.args[0])
        if not has_api_key(provider):
            item.add_marker(
                pytest.mark.skip(
                    reason=f"{KEY_VARS[provider]} not set -- live {provider} tests need a key"
                )
            )
