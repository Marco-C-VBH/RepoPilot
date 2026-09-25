# Base image for RepoPilot sandbox containers (spec §9.3).
#
# Layering plan:
#   base (this file)   python + git + pytest in a venv, unprivileged user, /workspace
#   └─ per-task image  clone repo @ base_commit, apply bug_patch, run env.install
#                      (network ON, built once, cached -- repopilot/sandbox/docker.py)
#        └─ container  fresh per run, --network none, CPU/memory/pids/time limits
#                      (repopilot/sandbox/limits.py)
#
# Built by repopilot.sandbox.docker.ensure_base_image(), which stages this file plus
# repopilot/sandbox/repopilot_pytest_plugin.py into a temporary build context.
ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash runner \
    && mkdir -p /workspace /opt/venv \
    && chown -R runner:runner /workspace /opt/venv

# pytest report plugin (exact node ids -> JSON). Root-owned on purpose: code running
# as `runner` can read it but cannot tamper with how results are recorded.
# COPY keeps the source file's mode, so force world-readable explicitly (a 0600
# checkout would otherwise make pytest fail with "Permission denied" as runner).
COPY repopilot_pytest_plugin.py /opt/repopilot/repopilot_pytest_plugin.py
RUN chmod 755 /opt/repopilot && chmod 644 /opt/repopilot/repopilot_pytest_plugin.py

USER runner
# Pinned on purpose (docs/issues.md #13): the test lists of every task were derived
# under this pytest, and a newer one can turn a repository's own test suite red --
# click 8.3.0 runs with `filterwarnings = ["error"]`, and pytest 9.1 warns at
# collection about a `parametrize` fed an iterator, so `tests/test_basic.py` stops
# collecting. Bump deliberately, then rebuild and re-derive.
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir "pytest==9.0.3"
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /workspace
