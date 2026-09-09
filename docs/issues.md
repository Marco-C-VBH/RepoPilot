# Issues log

Problems hit while building RepoPilot, with root cause and fix. One entry per
issue, newest first. The point is the same as the project's failure taxonomy:
a bug is only understood once its cause is written down and a test guards it.

Format: **symptom → root cause → fix → guard**.

---

## 2 · Docker integration tests skipped silently

**Date:** 2026-09-09 · **Area:** tests / sandbox · **Severity:** low

**Symptom.** `uv run pytest -m docker -v` reported `7 skipped` with no explanation,
even though Docker Desktop was installed. It was simply not running yet, but the
skip message ("no Docker daemon reachable") did not say *why* the probe failed,
and `-v` does not print skip reasons at all (`-rs` does).

**Root cause.** `docker_available()` collapsed every failure mode (CLI missing,
daemon down, timeout) into a bare `False`.

**Fix.** `repopilot/sandbox/docker.py`: new `docker_status() -> (available, detail)`
that returns the exact reason (CLI path, daemon error text, timeout);
`tests/conftest.py` puts that detail into the skip reason. `docker_available()`
is now a thin wrapper over it.

**Guard.** None needed — the diagnostic is the fix. Run Docker tests with `-rs`
to see skip reasons.

---

## 1 · pytest inside the sandbox could not read the report plugin

**Date:** 2026-09-09 · **Area:** sandbox image build · **Severity:** high (blocked
every `run_tests`)

**Symptom.** `test_gold_patch_turns_failing_tests_green_and_hidden_test_passes`
failed with `report_found == False`; the container's stderr ended in

```
PermissionError: [Errno 13] Permission denied: '/opt/repopilot/repopilot_pytest_plugin.py'
```

raised from pytest's assertion-rewrite import of the plugin. The other six
sandbox tests passed because none of them ran pytest.

**Root cause.** A chain of preserved file modes. The plugin file was written to
the working tree with mode `0600` (files delivered through the Cowork desktop
bridge land as owner-read/write only). `ensure_base_image` staged it into the
Docker build context with `shutil.copy`, which copies permission bits, and the
Dockerfile's `COPY` preserves the source mode as well. Result inside the image:
`/opt/repopilot/repopilot_pytest_plugin.py` owned by root with mode `0600` —
unreadable by the unprivileged `runner` user that executes pytest. The
root-ownership was deliberate (tamper resistance); the mode was an accident.

**Fix.**

- `docker/base.Dockerfile`: after the `COPY`, `RUN chmod 755 /opt/repopilot &&
  chmod 644 /opt/repopilot/repopilot_pytest_plugin.py` — world-readable, still
  root-owned.
- `repopilot/sandbox/docker.py` (`ensure_base_image`): use `shutil.copyfile`
  (content only, no mode bits) and `chmod(0o644)` on the staged copy, so a
  restrictive checkout mode can never reach the image again.

Because the base-image tag hashes the Dockerfile and the plugin source, the fix
rebuilt the base image automatically on the next run; the stale
`repopilot-base:3.11-<old hash>` can be removed with `docker rmi`.

**Guard.** `tests/test_sandbox_docker.py::test_snapshot_is_buggy_unprivileged_and_history_free`
now asserts that `runner` can `cat` the plugin file, so a permissions regression
fails in the first Docker test rather than surfacing as a mysterious
"no report" in `run_tests`.

**Lesson.** Anything copied into an image for another user to execute needs an
explicit mode; never rely on the mode the file happened to have on the host.
