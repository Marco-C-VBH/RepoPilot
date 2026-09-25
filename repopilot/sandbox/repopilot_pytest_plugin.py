"""pytest plugin loaded inside sandbox containers; writes a JSON test report.

Copied into the base image at /opt/repopilot/ and enabled per run with
``-p repopilot_pytest_plugin`` (PYTHONPATH=/opt/repopilot).  It executes inside
the *target repository's* environment, not RepoPilot's, so it must stay
dependency-free and work on any pytest >= 6 / Python >= 3.8.  Never import
from ``repopilot`` here.

Report written to $REPOPILOT_REPORT_PATH::

    {"version": 1,
     "exit_status": 1,
     "collection_errors": ["tests/test_broken.py"],
     "tests": {"tests/test_x.py::test_a": {"outcome": "failed",
                                            "duration": 0.012,
                                            "message": "AssertionError: ...",
                                            "longrepr": "def test_a(): ..."}}}

Outcome per test is the worst phase outcome: setup failure -> "error", call
failure -> "failed", teardown failure -> "error", skipped / xfailed ->
"skipped", otherwise "passed".  Exact pytest node ids are used as keys, so no
classname-to-path guessing is needed downstream -- with one normalization: the
path part of a node id is made relative to the directory pytest was invoked
from (the repository root in the sandbox).  pytest itself writes node ids
relative to its *rootdir*, which moves when a repository keeps an ini file in
its test directory (rich has ``tests/pytest.ini``): ``pytest tests/test_x.py``
then reports ``test_x.py::test_a``, and the task's ``tests/test_x.py::test_a``
would never match.
"""

import json
import os

REPORT_ENV = "REPOPILOT_REPORT_PATH"
DEFAULT_REPORT_PATH = "/tmp/repopilot_report.json"
REPORT_VERSION = 1

_RANK = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}
_MESSAGE_LIMIT = 2000
_LONGREPR_LIMIT = 8000

_tests = {}
_collection_errors = []
_rootdir = None
_invocation_dir = None


def _normalize(nodeid):
    """Make the path part of a node id relative to the invocation directory."""
    if not nodeid or _rootdir is None or _invocation_dir is None:
        return nodeid
    path, sep, rest = nodeid.partition("::")
    absolute = os.path.normpath(os.path.join(_rootdir, path))
    relative = os.path.relpath(absolute, _invocation_dir)
    if relative.startswith(".."):
        return nodeid  # outside the invocation directory: leave it alone
    return relative.replace(os.sep, "/") + sep + rest


def _truncate(text, limit):
    text = str(text)
    if len(text) > limit:
        return text[:limit] + "\n... [truncated by repopilot]"
    return text


def _messages(report):
    longrepr = getattr(report, "longrepr", None)
    if longrepr is None:
        return None, None
    if isinstance(longrepr, tuple):  # skip reports: (path, lineno, reason)
        reason = str(longrepr[-1])
        return _truncate(reason, _MESSAGE_LIMIT), None
    crash = getattr(longrepr, "reprcrash", None)
    short = getattr(crash, "message", None) or str(longrepr).strip().splitlines()[-1:]
    if isinstance(short, list):
        short = short[0] if short else ""
    return _truncate(short, _MESSAGE_LIMIT), _truncate(longrepr, _LONGREPR_LIMIT)


def _phase_outcome(report):
    if report.when == "call":
        return report.outcome  # passed | failed | skipped (xfail counts as skipped)
    if report.outcome == "failed":  # setup or teardown blew up
        return "error"
    if report.when == "setup" and report.outcome == "skipped":
        return "skipped"
    return None  # setup/teardown passed: carries no information


def pytest_sessionstart(session):
    global _rootdir, _invocation_dir
    _tests.clear()
    del _collection_errors[:]
    config = session.config
    rootpath = getattr(config, "rootpath", None) or getattr(config, "rootdir", None)
    _rootdir = str(rootpath) if rootpath is not None else None
    params = getattr(config, "invocation_params", None)
    _invocation_dir = str(params.dir) if params is not None else os.getcwd()


def pytest_runtest_logreport(report):
    nodeid = _normalize(report.nodeid)
    entry = _tests.get(nodeid)
    if entry is None:
        entry = {"outcome": None, "duration": 0.0, "message": None, "longrepr": None}
        _tests[nodeid] = entry
    entry["duration"] += float(getattr(report, "duration", 0.0) or 0.0)
    outcome = _phase_outcome(report)
    if outcome is None:
        return
    if entry["outcome"] is None or _RANK[outcome] > _RANK[entry["outcome"]]:
        entry["outcome"] = outcome
        entry["message"], entry["longrepr"] = _messages(report)


def pytest_collectreport(report):
    if report.failed:
        nodeid = _normalize(report.nodeid) or "<collection>"
        message, longrepr = _messages(report)
        _collection_errors.append(nodeid)
        _tests[nodeid] = {
            "outcome": "error",
            "duration": 0.0,
            "message": message,
            "longrepr": longrepr,
        }


def pytest_sessionfinish(session, exitstatus):
    for entry in _tests.values():
        if entry["outcome"] is None:
            entry["outcome"] = "error"
            entry["message"] = "no call phase reported (run interrupted?)"
    payload = {
        "version": REPORT_VERSION,
        "exit_status": int(exitstatus),
        "collection_errors": list(_collection_errors),
        "tests": _tests,
    }
    path = os.environ.get(REPORT_ENV, DEFAULT_REPORT_PATH)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
