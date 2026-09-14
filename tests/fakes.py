"""Shared stand-ins for the tool and agent tests (no Docker, no model)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from repopilot.sandbox.results import ExecResult, TestOutcome, TestResult, TestRun

# A small "buggy" project: Cache.put evicts one item too late (> should be >=).
DEMO_FILES = {
    "pyproject.toml": '[project]\nname = "demo"\n',
    "demo/__init__.py": "from demo.cache import Cache, DEFAULT_SIZE\n",
    "demo/cache.py": (
        "DEFAULT_SIZE = 3\n"
        "\n"
        "\n"
        "class Cache:\n"
        "    def __init__(self, size=DEFAULT_SIZE):\n"
        "        self.size = size\n"
        "        self.items = {}\n"
        "\n"
        "    def put(self, key, value):\n"
        "        if len(self.items) > self.size:\n"
        "            self.items.pop(next(iter(self.items)))\n"
        "        self.items[key] = value\n"
        "\n"
        "    def get(self, key, default=None):\n"
        "        return self.items.get(key, default)\n"
        "\n"
        "\n"
        "def make_cache(size=DEFAULT_SIZE):\n"
        "    def build():\n"
        "        return Cache(size)\n"
        "    return build()\n"
    ),
    "demo/util.py": (
        "try:\n    import json\nexcept ImportError:\n    json = None\n\n\n"
        "def helper():\n    return Cache\n"
    ),
    "tests/test_cache.py": (
        "from demo.cache import Cache\n\n\n"
        "def test_put_evicts():\n    c = Cache(size=1)\n    c.put('a', 1)\n    c.put('b', 2)\n"
        "    assert c.get('a') is None\n"
    ),
    "README.md": "# demo\n\nA Cache with a size limit.\n",
    "data.bin": "\0\0binary\0",
}


@dataclass
class FakeSandbox:
    """Records exec/apply/run calls and returns canned results."""

    run: TestRun
    apply_ok: bool = True
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def exec(self, command: Any, *, timeout: float | None = None) -> ExecResult:
        self.calls.append(("exec", command))
        return ExecResult(str(command), 0, "", "", 0.01)

    def apply_patch(self, patch: str, *, timeout: float = 60) -> ExecResult:
        self.calls.append(("apply", patch))
        return ExecResult("git apply", 0 if self.apply_ok else 1, "", "conflict", 0.01)

    def run_tests(self, test_command: str, *, timeout: float = 300) -> TestRun:
        self.calls.append(("run", test_command))
        return self.run


def make_run(**tests: str) -> TestRun:
    results = {
        nodeid: TestResult(
            nodeid,
            TestOutcome(outcome),
            message="AssertionError: assert None is None" if outcome == "failed" else None,
        )
        for nodeid, outcome in tests.items()
    }
    return TestRun(
        command="pytest",
        exit_code=0 if all(o == "passed" for o in tests.values()) else 1,
        timed_out=False,
        duration_seconds=0.5,
        stdout="",
        stderr="",
        tests=results,
        report_found=True,
    )
