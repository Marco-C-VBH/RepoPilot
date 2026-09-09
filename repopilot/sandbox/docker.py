"""Docker sandbox: per-task images and throwaway test containers (spec §9.3).

Two layers, so runs are fast, deterministic and offline:

* **Images** -- ``ensure_base_image`` builds ``repopilot-base:<python>-<hash>``
  from ``docker/base.Dockerfile`` (python + git + pytest in /opt/venv, an
  unprivileged ``runner`` user, the pytest report plugin under /opt/repopilot).
  ``build_task_image`` layers a repository snapshot on top: export the tree at
  ``base_commit`` host side (``repo.py``), COPY it in, ``git init`` a
  single-commit history, run the install command, and for mutation tasks apply
  ``bug_patch`` and amend it into that one commit.  Network is available during
  the build and never at run time.  Images are content-addressed by
  ``ImageSpec.cache_key`` and reused across runs.

* **Containers** -- ``Sandbox`` runs one container from a task image under
  ``SandboxLimits`` (``--network none``, CPU / memory / pids limits, all
  capabilities dropped) and exposes a small typed surface: ``exec``,
  ``apply_patch``, ``run_tests``, ``diff``.  ``run_tests`` loads the report
  plugin and returns a ``TestRun`` with exact per-node-id outcomes.

Everything goes through the ``docker`` CLI (subprocess): no SDK dependency, and
it behaves the same with Docker Desktop, OrbStack or colima.  This module takes
plain strings and patches rather than ``Task`` objects so the agent's
``run_tests`` tool (Phase 1) can reuse it unchanged.
"""

from __future__ import annotations

import hashlib
import math
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from repopilot.sandbox.limits import DEFAULT_LIMITS, SandboxLimits
from repopilot.sandbox.repo import DEFAULT_CACHE_DIR, export_tree
from repopilot.sandbox.results import ExecResult, TestRun, parse_report

DOCKER = shutil.which("docker")
REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_DOCKERFILE = REPO_ROOT / "docker" / "base.Dockerfile"
PLUGIN_SOURCE = Path(__file__).with_name("repopilot_pytest_plugin.py")
PLUGIN_NAME = "repopilot_pytest_plugin"
PLUGIN_DIR = "/opt/repopilot"
REPORT_PATH = "/tmp/repopilot_report.json"
WORKSPACE_REPO = "/workspace/repo"
LABEL = "repopilot.sandbox"
TASK_DOCKERFILE_VERSION = "1"
GIT_IDENTITY = ("-c", "user.name=repopilot", "-c", "user.email=repopilot@localhost")

# Exit codes of coreutils `timeout`: 124 = expired, 137 = killed after --kill-after.
_TIMEOUT_EXIT_CODES = frozenset({124, 137})
_OUTPUT_LIMIT = 200_000


class DockerError(RuntimeError):
    """The docker CLI returned an error."""


class DockerUnavailable(DockerError):
    """No docker CLI on PATH, or the daemon is not reachable."""


class ImageBuildError(DockerError):
    """``docker build`` failed; the message carries the tail of the build log."""


# -- CLI plumbing --------------------------------------------------------------


def docker_status() -> tuple[bool, str]:
    """``(available, detail)`` -- ``detail`` says why not, for skip messages and errors."""
    if not DOCKER:
        return False, "docker CLI not found on PATH"
    try:
        proc = subprocess.run(
            [DOCKER, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except OSError as exc:
        return False, f"docker CLI at {DOCKER} could not be started: {exc}"
    except subprocess.TimeoutExpired:
        return False, f"`{DOCKER} version` timed out after 20s (daemon hung or still starting?)"
    if proc.returncode != 0:
        detail = _tail(proc.stderr or proc.stdout, 300) or f"exit code {proc.returncode}"
        return False, f"docker daemon not reachable via {DOCKER}: {detail}"
    return True, f"docker server {proc.stdout.strip()} via {DOCKER}"


def docker_available() -> bool:
    """True when the docker CLI exists and a daemon answers."""
    return docker_status()[0]


def _docker(
    *args: str,
    stdin: str | None = None,
    timeout: float | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if not DOCKER:
        raise DockerUnavailable("docker CLI not found on PATH")
    try:
        proc = subprocess.run(
            [DOCKER, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - PATH changed under us
        raise DockerUnavailable("docker CLI not found on PATH") from exc
    if check and proc.returncode != 0:
        raise DockerError(
            f"docker {' '.join(args[:2])} failed ({proc.returncode}): {_tail(proc.stderr, 2000)}"
        )
    return proc


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else "...\n" + text[-limit:]


def _clip(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    clipped = len(text) - limit
    return f"{text[:half]}\n... [{clipped} bytes clipped by repopilot] ...\n{text[-half:]}"


def _hash(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def image_exists(tag: str) -> bool:
    return _docker("image", "inspect", tag, check=False).returncode == 0


def remove_image(tag: str) -> None:
    _docker("image", "rm", "--force", tag, check=False)


def sweep_containers() -> int:
    """Force-remove every container this module ever started; returns the count."""
    listing = _docker("ps", "--all", "--quiet", "--filter", f"label={LABEL}=container")
    ids = listing.stdout.split()
    if ids:
        _docker("rm", "--force", *ids, check=False)
    return len(ids)


def _build(context: Path, tag: str, build_args: Mapping[str, str] | None = None, timeout=1800):
    args = ["build", "--tag", tag, "--label", f"{LABEL}=image"]
    for key, value in (build_args or {}).items():
        args += ["--build-arg", f"{key}={value}"]
    args.append(str(context))
    proc = _docker(*args, timeout=timeout, check=False)
    if proc.returncode != 0:
        log = proc.stderr if proc.stderr.strip() else proc.stdout
        raise ImageBuildError(f"docker build of {tag} failed:\n{_tail(log, 6000)}")


# -- images ----------------------------------------------------------------------


def base_image_tag(python: str = "3.11") -> str:
    """Tag of the base image; changes whenever the Dockerfile or the plugin changes."""
    dockerfile = BASE_DOCKERFILE.read_text(encoding="utf-8")
    plugin = PLUGIN_SOURCE.read_text(encoding="utf-8")
    return f"repopilot-base:{python}-{_hash(dockerfile, plugin)[:10]}"


def ensure_base_image(python: str = "3.11", *, rebuild: bool = False) -> str:
    tag = base_image_tag(python)
    if not rebuild and image_exists(tag):
        return tag
    with tempfile.TemporaryDirectory(prefix="repopilot-base-") as tmp:
        context = Path(tmp)
        # copyfile, not copy: never carry a restrictive checkout mode into the image.
        shutil.copyfile(BASE_DOCKERFILE, context / "Dockerfile")
        shutil.copyfile(PLUGIN_SOURCE, context / PLUGIN_SOURCE.name)
        (context / PLUGIN_SOURCE.name).chmod(0o644)
        _build(context, tag, build_args={"PYTHON_VERSION": python})
    return tag


@dataclass(frozen=True)
class ImageSpec:
    """Everything that determines a task image (and therefore its cache key)."""

    repo: str
    base_commit: str
    python: str = "3.11"
    install: str = "pip install -e ."
    bug_patch: str | None = None

    def cache_key(self) -> str:
        return _hash(
            TASK_DOCKERFILE_VERSION,
            base_image_tag(self.python),
            self.repo,
            self.base_commit,
            self.install,
            self.bug_patch or "",
        )[:12]

    @property
    def tag(self) -> str:
        return f"repopilot-task:{self.cache_key()}"


def render_task_dockerfile(spec: ImageSpec, base_tag: str) -> str:
    git = " ".join(("git", *GIT_IDENTITY))
    lines = [
        f"FROM {base_tag}",
        f"COPY --chown=runner:runner repo/ {WORKSPACE_REPO}/",
        f"WORKDIR {WORKSPACE_REPO}",
        # One synthetic commit: no upstream history, nothing to leak through git log.
        f"RUN git init -q -b main && git add -A && {git} commit -q --allow-empty -m base",
        f"RUN {spec.install}",
    ]
    if spec.bug_patch is not None:
        lines += [
            "COPY --chown=runner:runner bug.patch /tmp/bug.patch",
            # Amend the injected bug into that same single commit, then drop the patch file.
            f"RUN git apply --index --whitespace=nowarn /tmp/bug.patch && {git} commit -q "
            "--amend -m base && rm -f /tmp/bug.patch",
        ]
    return "\n".join(lines) + "\n"


def build_task_image(
    spec: ImageSpec,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    rebuild: bool = False,
    build_timeout: float = 1800,
) -> str:
    """Build (or reuse) the image for ``spec``; returns its tag."""
    tag = spec.tag
    if not rebuild and image_exists(tag):
        return tag
    base_tag = ensure_base_image(spec.python)
    with tempfile.TemporaryDirectory(prefix="repopilot-task-") as tmp:
        context = Path(tmp)
        export_tree(spec.repo, spec.base_commit, context / "repo", cache_dir=cache_dir)
        dockerfile = render_task_dockerfile(spec, base_tag)
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        if spec.bug_patch is not None:
            (context / "bug.patch").write_text(_with_newline(spec.bug_patch), encoding="utf-8")
        _build(context, tag, timeout=build_timeout)
    return tag


def _with_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


# -- containers ------------------------------------------------------------------


class Sandbox:
    """One throwaway container.  Use as a context manager::

    with Sandbox(image) as sb:
        sb.apply_patch(candidate)
        run = sb.run_tests("pytest tests/test_x.py", timeout=300)
    """

    def __init__(
        self,
        image: str,
        limits: SandboxLimits = DEFAULT_LIMITS,
        *,
        workdir: str = WORKSPACE_REPO,
    ) -> None:
        self.image = image
        self.limits = limits
        self.workdir = workdir
        self.name: str | None = None

    def __enter__(self) -> Sandbox:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def running(self) -> bool:
        return self.name is not None

    def start(self) -> Sandbox:
        if self.name is not None:
            return self
        name = f"repopilot-{uuid4().hex[:12]}"
        _docker(
            "run",
            "--detach",
            "--init",
            "--name",
            name,
            "--label",
            f"{LABEL}=container",
            *self.limits.docker_run_args(),
            "--workdir",
            self.workdir,
            self.image,
            "sleep",
            "infinity",
        )
        self.name = name
        return self

    def close(self) -> None:
        if self.name is not None:
            _docker("rm", "--force", self.name, check=False)
            self.name = None

    def exec(
        self,
        command: str | Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        workdir: str | None = None,
    ) -> ExecResult:
        """Run a command as the unprivileged container user.

        A string is run through ``sh -c``; a sequence is exec'd directly.  With
        ``timeout`` the command is wrapped in coreutils ``timeout`` inside the
        container (exit 124/137 -> ``timed_out``); if even that fails to return,
        the container is killed and closed.
        """
        if self.name is None:
            raise DockerError("sandbox is not running")
        inner = ["sh", "-c", command] if isinstance(command, str) else list(command)
        display = command if isinstance(command, str) else " ".join(inner)
        if timeout is not None:
            inner = ["timeout", "--kill-after=5", str(math.ceil(timeout)), *inner]
        args = ["exec", "--workdir", workdir or self.workdir]
        for key, value in (env or {}).items():
            args += ["--env", f"{key}={value}"]
        if stdin is not None:
            args.append("--interactive")
        args += [self.name, *inner]

        started = time.monotonic()
        try:
            proc = _docker(
                *args,
                stdin=stdin,
                timeout=None if timeout is None else timeout + 30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # In-container `timeout` did not fire; the container is not trustworthy anymore.
            self.close()
            return ExecResult(
                command=display,
                exit_code=124,
                stdout=_clip(_as_text(exc.stdout)),
                stderr=_clip(_as_text(exc.stderr)) + "\n[repopilot] container killed after timeout",
                duration_seconds=time.monotonic() - started,
                timed_out=True,
            )
        return ExecResult(
            command=display,
            exit_code=proc.returncode,
            stdout=_clip(proc.stdout),
            stderr=_clip(proc.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=timeout is not None and proc.returncode in _TIMEOUT_EXIT_CODES,
        )

    def apply_patch(self, patch: str, *, timeout: float = 60) -> ExecResult:
        """Apply a unified diff to the working tree (``git apply``); rejects on conflict."""
        return self.exec(
            ["git", "apply", "--whitespace=nowarn", "-"],
            stdin=_with_newline(patch),
            timeout=timeout,
        )

    def run_tests(self, test_command: str, *, timeout: float = 300) -> TestRun:
        """Run a pytest command with the report plugin and collect per-test outcomes.

        ``-p repopilot_pytest_plugin`` (exact node ids), ``-p no:cacheprovider``
        (keeps .pytest_cache out of the working tree) and
        ``--continue-on-collection-errors`` (one broken module must not hide the
        results of the others) are appended when the command is a pytest command.
        """
        command = test_command
        if "pytest" in test_command:
            command = (
                f"{test_command} -p {PLUGIN_NAME} -p no:cacheprovider "
                "--continue-on-collection-errors"
            )
        self.exec(["rm", "-f", REPORT_PATH], timeout=10)  # never read a stale report
        result = self.exec(
            command,
            timeout=timeout,
            env={"PYTHONPATH": PLUGIN_DIR, "REPOPILOT_REPORT_PATH": REPORT_PATH},
        )
        report = None
        if self.running:
            fetched = self.exec(["cat", REPORT_PATH], timeout=10)
            if fetched.ok:
                try:
                    report = parse_report(fetched.stdout)
                except ValueError:
                    report = None
        return TestRun(
            command=command,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_seconds=result.duration_seconds,
            stdout=result.stdout,
            stderr=result.stderr,
            tests=report.tests if report else {},
            report_found=report is not None,
            pytest_exit_status=report.exit_status if report else None,
            collection_errors=report.collection_errors if report else (),
        )

    def diff(self) -> str:
        """Unified diff of the working tree against the task's base commit (new files included)."""
        self.exec(["git", "add", "--intent-to-add", "."], timeout=60)
        result = self.exec(["git", "diff", "HEAD"], timeout=60)
        if not result.ok:
            raise DockerError(f"git diff failed: {result.stderr.strip()}")
        return result.stdout


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
