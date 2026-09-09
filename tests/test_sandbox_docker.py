"""End-to-end sandbox tests against a real Docker daemon (``-m docker``).

A tiny local git repository plays the target project: a clean ``clamp()``
implementation, a mutation that injects an off-by-one, the gold patch that
reverts it, and a hidden regression test.  The first run builds the base image
(pulls python:3.11-slim, installs git + pytest) and takes a couple of minutes;
later runs reuse it.  The task image built here is removed afterwards.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from repopilot.sandbox.docker import (
    DOCKER,
    ImageSpec,
    Sandbox,
    build_task_image,
    remove_image,
)
from repopilot.sandbox.results import TestOutcome
from tests.fixture_repo import (
    ABOVE,
    BELOW,
    BUG_PATCH,
    GOLD_PATCH,
    HIDDEN,
    HIDDEN_TEST_PATCH,
    INSIDE,
    TEST_COMMAND,
    create_fixture_repo,
)

pytestmark = pytest.mark.docker



@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    path = tmp_path_factory.mktemp("fixture-repo") / "repo"
    return path, create_fixture_repo(path)


@pytest.fixture(scope="module")
def task_image(
    fixture_repo: tuple[Path, str], tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    path, sha = fixture_repo
    spec = ImageSpec(
        repo=str(path),
        base_commit=sha,
        python="3.11",
        install="true",
        bug_patch=BUG_PATCH,
    )
    tag = build_task_image(spec, cache_dir=tmp_path_factory.mktemp("cache"))
    yield tag
    remove_image(tag)


@pytest.fixture
def sandbox(task_image: str) -> Iterator[Sandbox]:
    with Sandbox(task_image) as sb:
        yield sb


def test_snapshot_is_buggy_unprivileged_and_history_free(sandbox: Sandbox) -> None:
    source = sandbox.exec(["cat", "fixturepkg/__init__.py"])
    assert source.ok
    assert "high - 1" in source.stdout, "bug_patch must be baked into the image"

    assert sandbox.exec(["whoami"]).stdout.strip() == "runner"
    log = sandbox.exec(["git", "log", "--oneline"])
    assert log.ok and len(log.stdout.strip().splitlines()) == 1, "history must be one commit"
    assert sandbox.exec(["git", "status", "--porcelain"]).stdout.strip() == ""
    assert not sandbox.exec(["test", "-e", "/tmp/bug.patch"]).ok

    plugin = sandbox.exec(["cat", "/opt/repopilot/repopilot_pytest_plugin.py"])
    assert plugin.ok, f"runner must be able to read the report plugin: {plugin.stderr}"
    assert "pytest_sessionfinish" in plugin.stdout


def test_gold_patch_turns_failing_tests_green_and_hidden_test_passes(sandbox: Sandbox) -> None:
    before = sandbox.run_tests(TEST_COMMAND, timeout=120)
    assert before.report_found, before.stderr
    assert before.exit_code == 1 and before.pytest_exit_status == 1
    assert before.outcome(ABOVE) is TestOutcome.FAILED
    assert before.outcome(INSIDE) is TestOutcome.PASSED
    assert before.outcome(BELOW) is TestOutcome.PASSED
    assert "assert 9 == 10" in (before.tests[ABOVE].message or "")
    assert before.counts()["failed"] == 1

    applied = sandbox.apply_patch(GOLD_PATCH)
    assert applied.ok, applied.stderr

    after = sandbox.run_tests(TEST_COMMAND, timeout=120)
    assert after.exit_code == 0, after.stdout
    assert all(after.passed(t) for t in (ABOVE, INSIDE, BELOW))
    assert not after.passed(HIDDEN), "hidden test must not exist before its patch is applied"

    assert sandbox.apply_patch(HIDDEN_TEST_PATCH).ok
    hidden = sandbox.run_tests(TEST_COMMAND, timeout=120)
    assert hidden.passed(HIDDEN)
    assert hidden.counts() == {"passed": 4, "failed": 0, "error": 0, "skipped": 0}

    diff = sandbox.diff()
    assert "+    return max(low, min(value, high))" in diff
    assert "tests/test_hidden.py" in diff
    assert ".pytest_cache" not in diff and "__pycache__" not in diff


def test_conflicting_patch_is_rejected_without_side_effects(sandbox: Sandbox) -> None:
    assert sandbox.apply_patch(GOLD_PATCH).ok
    again = sandbox.apply_patch(GOLD_PATCH)
    assert not again.ok
    assert "does not apply" in again.stderr
    assert sandbox.exec(["git", "diff", "--stat"]).stdout.count("fixturepkg/__init__.py") == 1


def test_network_is_disabled(sandbox: Sandbox) -> None:
    probe = sandbox.exec(
        ["python", "-c", "import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)"],
        timeout=20,
    )
    assert not probe.ok
    assert "unreachable" in probe.stderr.lower() or "error" in probe.stderr.lower()


def test_writes_outside_the_workspace_are_refused(sandbox: Sandbox) -> None:
    assert not sandbox.exec(["touch", "/etc/repopilot-escape"]).ok
    assert not sandbox.exec(["touch", "/opt/repopilot/tamper.py"]).ok
    assert sandbox.exec(["touch", "/workspace/repo/scratch.txt"]).ok


def test_timeouts_are_enforced_inside_the_container(sandbox: Sandbox) -> None:
    run = sandbox.run_tests("sleep 30", timeout=1)
    assert run.timed_out
    assert run.exit_code in (124, 137)
    assert not run.report_found
    assert run.duration_seconds < 20
    assert "TIMED OUT" in run.summary()
    assert sandbox.running, "an in-container timeout must not tear the sandbox down"


def test_container_is_removed_on_close(task_image: str) -> None:
    sb = Sandbox(task_image).start()
    name = sb.name
    assert name is not None
    sb.close()
    assert not sb.running
    listing = subprocess.run(
        [DOCKER or "docker", "ps", "--all", "--quiet", "--filter", f"name={name}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert listing.stdout.strip() == ""
