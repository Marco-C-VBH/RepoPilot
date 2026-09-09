"""A tiny target project for the Docker end-to-end tests.

Clean ``clamp()`` at the base commit, a mutation that injects an off-by-one,
the gold patch that reverts it, and a hidden regression test -- everything a
real RepoPilot-Bench task has, small enough to build in seconds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.gitfixtures import init_repo

FILES = {
    "pyproject.toml": (
        '[project]\nname = "fixturepkg"\nversion = "0.0.1"\n\n'
        '[tool.pytest.ini_options]\npythonpath = ["."]\n'
    ),
    "fixturepkg/__init__.py": (
        "def clamp(value, low, high):\n"
        '    """Clamp value into [low, high]."""\n'
        "    return max(low, min(value, high))\n"
    ),
    "tests/test_clamp.py": (
        "from fixturepkg import clamp\n\n\n"
        "def test_inside():\n    assert clamp(5, 0, 10) == 5\n\n\n"
        "def test_above():\n    assert clamp(15, 0, 10) == 10\n\n\n"
        "def test_below():\n    assert clamp(-5, 0, 10) == 0\n"
    ),
}

BUG_PATCH = """\
diff --git a/fixturepkg/__init__.py b/fixturepkg/__init__.py
--- a/fixturepkg/__init__.py
+++ b/fixturepkg/__init__.py
@@ -1,3 +1,3 @@
 def clamp(value, low, high):
     \"\"\"Clamp value into [low, high].\"\"\"
-    return max(low, min(value, high))
+    return max(low, min(value, high - 1))
"""

GOLD_PATCH = """\
diff --git a/fixturepkg/__init__.py b/fixturepkg/__init__.py
--- a/fixturepkg/__init__.py
+++ b/fixturepkg/__init__.py
@@ -1,3 +1,3 @@
 def clamp(value, low, high):
     \"\"\"Clamp value into [low, high].\"\"\"
-    return max(low, min(value, high - 1))
+    return max(low, min(value, high))
"""

# Removes clamping altogether: fixes nothing and breaks test_below (a regression).
WRONG_PATCH = """\
diff --git a/fixturepkg/__init__.py b/fixturepkg/__init__.py
--- a/fixturepkg/__init__.py
+++ b/fixturepkg/__init__.py
@@ -1,3 +1,3 @@
 def clamp(value, low, high):
     \"\"\"Clamp value into [low, high].\"\"\"
-    return max(low, min(value, high - 1))
+    return value
"""

HIDDEN_TEST_PATCH = """\
diff --git a/tests/test_hidden.py b/tests/test_hidden.py
new file mode 100644
--- /dev/null
+++ b/tests/test_hidden.py
@@ -0,0 +1,5 @@
+from fixturepkg import clamp
+
+
+def test_upper_bound_is_inclusive():
+    assert clamp(10, 0, 10) == 10
"""

# Well-formed diff that cannot apply: the file it edits does not exist.
UNAPPLIABLE_PATCH = """\
diff --git a/tests/test_missing.py b/tests/test_missing.py
--- a/tests/test_missing.py
+++ b/tests/test_missing.py
@@ -1,2 +1,2 @@
 import fixturepkg
-x = 1
+x = 2
"""

TEST_COMMAND = "pytest tests"
ABOVE = "tests/test_clamp.py::test_above"
INSIDE = "tests/test_clamp.py::test_inside"
BELOW = "tests/test_clamp.py::test_below"
HIDDEN = "tests/test_hidden.py::test_upper_bound_is_inclusive"


def create_fixture_repo(path: Path) -> str:
    """Create the clean project as a git repository; returns the base commit SHA."""
    return init_repo(path, FILES)


def fixture_task_dict(repo_path: Path, sha: str, task_id: str = "fixture_001") -> dict[str, Any]:
    """A complete, schema-valid task pointing at the fixture repository."""
    return {
        "id": task_id,
        "repo": str(repo_path),
        "base_commit": sha,
        "source": "mutation",
        "description": (
            "clamp() returns a value one below the upper bound: clamp(15, 0, 10) gives 9 "
            "instead of 10. Find the cause and fix it."
        ),
        "bug_patch": BUG_PATCH,
        "gold_patch": GOLD_PATCH,
        "hidden_test_patch": HIDDEN_TEST_PATCH,
        "fail_to_pass": [ABOVE, HIDDEN],
        "pass_to_pass": [INSIDE, BELOW],
        "test_command": TEST_COMMAND,
        "gold_files": ["fixturepkg/__init__.py"],
        "gold_symbols": ["clamp"],
        "category": "off_by_one",
        "difficulty": "easy",
        "env": {"python": "3.11", "install": "true", "test_timeout_seconds": 120},
    }
