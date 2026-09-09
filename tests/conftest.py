"""Shared test configuration.

* ``pytester`` is enabled so the in-container pytest report plugin can be
  exercised in a real (subprocess) pytest session.
* Tests marked ``@pytest.mark.docker`` need a Docker daemon and are skipped
  automatically when none is reachable (or when REPOPILOT_SKIP_DOCKER is set).
"""

from __future__ import annotations

import os

import pytest

from repopilot.sandbox.docker import docker_status

pytest_plugins = ["pytester"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
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
