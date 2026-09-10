"""make_task end to end against Docker: fixture source dir -> validated task JSON."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from evals.benchmark.authoring import AuthoringError, make_task, report
from evals.benchmark.registry import load_tasks
from evals.harness import evaluate_patch, image_spec_for
from evals.judge import Status
from repopilot.sandbox.docker import remove_image
from tests.fixture_repo import ABOVE, BELOW, HIDDEN, INSIDE, create_fixture_repo
from tests.test_authoring import write_source

pytestmark = pytest.mark.docker

USELESS_HIDDEN_PATCH = """\
diff --git a/tests/test_hidden.py b/tests/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/tests/test_hidden.py
@@ -0,0 +1,5 @@
+from fixturepkg import clamp
+
+
+def test_inside_again():
+    assert clamp(5, 0, 10) == 5
"""


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("authoring")
    sha = create_fixture_repo(root / "repo")
    (root / "sha").write_text(sha)
    yield root
    tasks = root / "tasks"
    if tasks.is_dir():
        for task in load_tasks(tasks):
            remove_image(image_spec_for(task).tag)


def test_make_task_derives_everything_from_runs(workspace: Path) -> None:
    repo, sha = workspace / "repo", (workspace / "sha").read_text()
    source = write_source(workspace / "sources" / "fixture_001", repo, sha)
    lines: list[str] = []

    result = make_task(
        source, out_dir=workspace / "tasks", cache_dir=workspace / "cache", log=lines.append
    )

    assert result.path == workspace / "tasks" / "fixture_001.json"
    task = load_tasks(workspace / "tasks")[0]
    assert task == result.task
    assert task.fail_to_pass == [ABOVE, HIDDEN]
    assert task.pass_to_pass == [BELOW, INSIDE]
    assert task.gold_files == ["fixturepkg/__init__.py"]
    assert task.gold_symbols == ["clamp"]
    assert task.hidden_test_files == ("tests/test_hidden.py",)
    assert result.derived.excluded == {}
    assert result.derived.buggy_run.counts()["failed"] == 2
    assert result.derived.fixed_run.counts()["passed"] == 4

    text = report(result)
    assert "[hidden ] tests/test_hidden.py::test_upper_bound_is_inclusive" in text
    assert "[visible] tests/test_clamp.py::test_above" in text
    assert any("building image" in line for line in lines)

    # The derived task is judged exactly as the runner will judge it.
    assert evaluate_patch(task, result.image, task.gold_patch).status is Status.PASS
    assert evaluate_patch(task, result.image, None).status is Status.FAIL


def test_existing_task_needs_force(workspace: Path) -> None:
    repo, sha = workspace / "repo", (workspace / "sha").read_text()
    source = write_source(workspace / "sources" / "fixture_001", repo, sha)
    with pytest.raises(AuthoringError, match="already exists"):
        make_task(
            source, out_dir=workspace / "tasks", cache_dir=workspace / "cache", log=lambda _: None
        )
    result = make_task(
        source,
        out_dir=workspace / "tasks",
        cache_dir=workspace / "cache",
        dry_run=True,
        log=lambda _: None,
    )
    assert result.path is None


def test_hidden_test_that_ignores_the_bug_is_rejected(workspace: Path) -> None:
    repo, sha = workspace / "repo", (workspace / "sha").read_text()
    source = write_source(
        workspace / "sources" / "fixture_002",
        repo,
        sha,
        task_id="fixture_002",
        hidden_patch=USELESS_HIDDEN_PATCH,
    )
    with pytest.raises(AuthoringError, match="hidden tests must fail with the bug"):
        make_task(
            source, out_dir=workspace / "tasks", cache_dir=workspace / "cache", log=lambda _: None
        )
    assert not (workspace / "tasks" / "fixture_002.json").exists()
