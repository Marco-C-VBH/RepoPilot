"""Docker sandbox -- Phase 0, step 2 (not implemented yet).

Flow (spec §9.3), two layers so runs are fast and deterministic:

1. ``build_task_image(...)``  -- network ON, built once, cached by
   (repo, base_commit, bug_patch, env).  Starts from ``docker/base.Dockerfile``,
   clones the repository, checks out ``base_commit``, applies ``bug_patch`` for
   mutation tasks, runs ``env.install``.

2. ``run_tests(...)``  -- a fresh container from that image for every run, with
   ``SandboxLimits.docker_run_args()`` and ``--network none``.  Applies the
   candidate patch (and ``hidden_test_patch`` at evaluation time), runs
   ``<test_command> --junitxml=<workspace>/.repopilot/junit.xml`` under the
   wall-clock timeout, copies junit.xml + stdout/stderr out, destroys the
   container, and parses junit into ``{test_id: "passed" | "failed" | ...}``.

Design rule: this module takes plain strings and patches, not ``Task`` objects,
so the agent's ``run_tests`` tool (``repopilot/tools/tests.py``, Phase 1) can
reuse it unchanged.
"""

from __future__ import annotations
