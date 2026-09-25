"""The in-container pytest report plugin, run in a real subprocess pytest session."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repopilot.sandbox.docker import PLUGIN_NAME, PLUGIN_SOURCE

SAMPLE_TESTS = """
import pytest


def test_ok():
    assert True


def test_fails():
    assert 1 == 2, "one is not two"


@pytest.fixture
def broken():
    raise RuntimeError("boom in setup")


def test_setup_error(broken):
    pass


@pytest.mark.skip(reason="not today")
def test_skipped():
    pass


@pytest.mark.xfail(reason="known bug")
def test_xfail():
    assert False


@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError("boom in teardown")


def test_teardown_error(bad_teardown):
    pass


@pytest.mark.parametrize("x", [1, 2])
def test_param(x):
    assert x < 2
"""


@pytest.fixture
def report_path(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "report.json"
    monkeypatch.setenv("REPOPILOT_REPORT_PATH", str(path))
    monkeypatch.setenv("PYTHONPATH", str(PLUGIN_SOURCE.parent))
    return path


def run_with_plugin(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest_subprocess("-p", PLUGIN_NAME, "-q", *args)


def test_outcomes_use_exact_node_ids(pytester: pytest.Pytester, report_path: Path) -> None:
    pytester.makepyfile(test_sample=SAMPLE_TESTS)
    result = run_with_plugin(pytester)
    assert result.ret == 1  # some tests failed

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["exit_status"] == 1
    assert data["collection_errors"] == []

    outcomes = {nodeid: entry["outcome"] for nodeid, entry in data["tests"].items()}
    assert outcomes == {
        "test_sample.py::test_ok": "passed",
        "test_sample.py::test_fails": "failed",
        "test_sample.py::test_setup_error": "error",
        "test_sample.py::test_skipped": "skipped",
        "test_sample.py::test_xfail": "skipped",
        "test_sample.py::test_teardown_error": "error",
        "test_sample.py::test_param[1]": "passed",
        "test_sample.py::test_param[2]": "failed",
    }
    failed = data["tests"]["test_sample.py::test_fails"]
    assert "one is not two" in failed["message"]
    assert "def test_fails" in failed["longrepr"]
    assert "boom in setup" in data["tests"]["test_sample.py::test_setup_error"]["message"]
    assert "not today" in data["tests"]["test_sample.py::test_skipped"]["message"]
    assert data["tests"]["test_sample.py::test_ok"]["message"] is None
    assert all(entry["duration"] >= 0 for entry in data["tests"].values())


def test_collection_error_is_recorded_and_other_modules_still_run(
    pytester: pytest.Pytester, report_path: Path
) -> None:
    pytester.makepyfile(test_good="def test_fine():\n    assert True\n")
    pytester.makepyfile(test_broken="import module_that_does_not_exist_xyz\n")
    result = run_with_plugin(pytester, "--continue-on-collection-errors")
    assert result.ret != 0

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["collection_errors"] == ["test_broken.py"]
    assert data["tests"]["test_broken.py"]["outcome"] == "error"
    assert "module_that_does_not_exist_xyz" in data["tests"]["test_broken.py"]["longrepr"]
    assert data["tests"]["test_good.py::test_fine"]["outcome"] == "passed"


def test_report_is_written_even_when_nothing_is_collected(
    pytester: pytest.Pytester, report_path: Path
) -> None:
    result = run_with_plugin(pytester)
    assert result.ret == 5  # pytest: no tests collected
    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["exit_status"] == 5
    assert data["tests"] == {}


def test_node_ids_are_relative_to_the_invocation_directory(
    pytester: pytest.Pytester, report_path: Path
) -> None:
    """An ini file inside the test directory moves pytest's rootdir (rich keeps
    ``tests/pytest.ini``); the report must still key on ``tests/test_x.py::...``."""
    tests = pytester.mkdir("tests")
    (tests / "pytest.ini").write_text("[pytest]\njunit_family=legacy\n")
    (tests / "__init__.py").write_text("")
    (tests / "test_inner.py").write_text("def test_a():\n    assert True\n")
    (tests / "test_broken.py").write_text("import module_that_does_not_exist_xyz\n")
    result = run_with_plugin(
        pytester, "--continue-on-collection-errors", "tests/test_inner.py", "tests/test_broken.py"
    )
    assert result.ret != 0

    data = json.loads(report_path.read_text(encoding="utf-8"))
    assert data["tests"]["tests/test_inner.py::test_a"]["outcome"] == "passed"
    assert data["collection_errors"] == ["tests/test_broken.py"]
