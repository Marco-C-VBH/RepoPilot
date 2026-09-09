"""Sandboxed execution: Docker image build, throwaway test containers, resource limits."""

from repopilot.sandbox.docker import (
    DockerError,
    DockerUnavailable,
    ImageBuildError,
    ImageSpec,
    Sandbox,
    build_task_image,
    docker_available,
    ensure_base_image,
    sweep_containers,
)
from repopilot.sandbox.limits import DEFAULT_LIMITS, SandboxLimits
from repopilot.sandbox.repo import RepoError, export_tree
from repopilot.sandbox.results import ExecResult, TestOutcome, TestResult, TestRun

__all__ = [
    "DEFAULT_LIMITS",
    "DockerError",
    "DockerUnavailable",
    "ExecResult",
    "ImageBuildError",
    "ImageSpec",
    "RepoError",
    "Sandbox",
    "SandboxLimits",
    "TestOutcome",
    "TestResult",
    "TestRun",
    "build_task_image",
    "docker_available",
    "ensure_base_image",
    "export_tree",
    "sweep_containers",
]
