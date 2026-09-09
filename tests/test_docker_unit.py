"""Docker sandbox pieces that need no daemon: cache keys, Dockerfile rendering, results."""

from __future__ import annotations

import json

import pytest

from repopilot.sandbox.docker import (
    BASE_DOCKERFILE,
    PLUGIN_SOURCE,
    DockerError,
    ImageSpec,
    Sandbox,
    base_image_tag,
    render_task_dockerfile,
)
from repopilot.sandbox.results import TestOutcome, TestRun, parse_report

BUG_PATCH = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-ok = True\n+ok = False\n"
)


def test_base_image_tag_tracks_dockerfile_and_plugin() -> None:
    assert BASE_DOCKERFILE.is_file()
    assert PLUGIN_SOURCE.is_file()
    tag = base_image_tag("3.11")
    assert tag.startswith("repopilot-base:3.11-")
    assert tag == base_image_tag("3.11")
    assert tag != base_image_tag("3.12")


def test_cache_key_is_content_addressed() -> None:
    spec = ImageSpec(repo="https://github.com/example/demo", base_commit="a" * 40)
    assert spec.cache_key() == spec.cache_key()
    assert spec.tag == f"repopilot-task:{spec.cache_key()}"
    assert len(spec.cache_key()) == 12

    variants = [
        ImageSpec(repo=spec.repo, base_commit="b" * 40),
        ImageSpec(repo=spec.repo, base_commit=spec.base_commit, install="pip install -e .[test]"),
        ImageSpec(repo=spec.repo, base_commit=spec.base_commit, python="3.12"),
        ImageSpec(repo=spec.repo, base_commit=spec.base_commit, bug_patch=BUG_PATCH),
    ]
    keys = {spec.cache_key(), *(v.cache_key() for v in variants)}
    assert len(keys) == len(variants) + 1


def test_task_dockerfile_for_real_task() -> None:
    spec = ImageSpec(repo="r", base_commit="a" * 40, install="pip install -e .[test]")
    text = render_task_dockerfile(spec, "repopilot-base:3.11-abc")
    lines = text.splitlines()
    assert lines[0] == "FROM repopilot-base:3.11-abc"
    assert "COPY --chown=runner:runner repo/ /workspace/repo/" in lines
    assert "WORKDIR /workspace/repo" in lines
    assert any("git init -q -b main" in line and "commit -q" in line for line in lines)
    assert "RUN pip install -e .[test]" in lines
    assert "bug.patch" not in text


def test_task_dockerfile_for_mutation_task_amends_bug_into_single_commit() -> None:
    spec = ImageSpec(repo="r", base_commit="a" * 40, bug_patch=BUG_PATCH)
    text = render_task_dockerfile(spec, "base")
    assert "COPY --chown=runner:runner bug.patch /tmp/bug.patch" in text
    assert "git apply --index" in text
    assert "--amend" in text
    assert "rm -f /tmp/bug.patch" in text
    assert text.index("RUN pip install -e .") < text.index("git apply"), (
        "install runs before the bug is injected so the layer is shared across mutations"
    )


def test_sandbox_refuses_exec_when_not_started() -> None:
    with pytest.raises(DockerError, match="not running"):
        Sandbox("some-image").exec("true")


def test_parse_report_maps_outcomes_and_tolerates_unknown_values() -> None:
    payload = {
        "version": 1,
        "exit_status": 1,
        "collection_errors": ["tests/test_broken.py"],
        "tests": {
            "tests/test_a.py::test_ok": {"outcome": "passed", "duration": 0.5},
            "tests/test_a.py::test_bad": {"outcome": "failed", "message": "AssertionError"},
            "tests/test_broken.py": {"outcome": "error"},
            "tests/test_a.py::weird": {"outcome": "mystery"},
        },
    }
    report = parse_report(json.dumps(payload))
    assert report.exit_status == 1
    assert report.collection_errors == ("tests/test_broken.py",)
    assert report.tests["tests/test_a.py::test_ok"].outcome is TestOutcome.PASSED
    assert report.tests["tests/test_a.py::test_ok"].duration == 0.5
    assert report.tests["tests/test_a.py::test_bad"].message == "AssertionError"
    assert report.tests["tests/test_a.py::weird"].outcome is TestOutcome.ERROR


def test_test_run_treats_missing_node_ids_as_not_passed() -> None:
    report = parse_report(
        json.dumps({"tests": {"t.py::a": {"outcome": "passed"}, "t.py::b": {"outcome": "failed"}}})
    )
    run = TestRun(
        command="pytest",
        exit_code=1,
        timed_out=False,
        duration_seconds=1.234,
        stdout="",
        stderr="",
        tests=report.tests,
        report_found=True,
        pytest_exit_status=1,
    )
    assert run.passed("t.py::a")
    assert not run.passed("t.py::b")
    assert not run.passed("t.py::never_ran")
    assert run.outcome("t.py::never_ran") is None
    assert run.counts() == {"passed": 1, "failed": 1, "error": 0, "skipped": 0}
    assert run.summary() == "1 passed, 1 failed, 0 errors, 0 skipped · exit 1 · 1.2s"


def test_test_run_summary_flags_timeouts_and_missing_reports() -> None:
    run = TestRun(
        command="pytest", exit_code=124, timed_out=True, duration_seconds=300, stdout="", stderr=""
    )
    assert run.summary().endswith("· TIMED OUT · no report")
    assert run.counts() == {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
