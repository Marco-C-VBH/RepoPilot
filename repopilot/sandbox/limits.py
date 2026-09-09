"""Resource limits for sandbox containers (spec §9.1, §9.3).

Every benchmark configuration must run under identical, documented limits so
that comparisons between configurations stay meaningful.  ``DEFAULT_LIMITS`` is
the single source of truth; experiments that change it must say so.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SandboxLimits:
    """Hard limits applied to every ``docker run`` (network off by default)."""

    cpus: float = 1.0
    memory: str = "2g"
    pids_limit: int = 256
    network: str = "none"
    timeout_seconds: int = 600
    workspace: str = "/workspace"

    def __post_init__(self) -> None:
        if self.cpus <= 0:
            raise ValueError("cpus must be positive")
        if self.pids_limit <= 0:
            raise ValueError("pids_limit must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not self.workspace.startswith("/"):
            raise ValueError("workspace must be an absolute path inside the container")

    def docker_run_args(self) -> list[str]:
        """Flags for ``docker run``; wall-clock timeout is enforced by the caller."""
        return [
            "--cpus",
            str(self.cpus),
            "--memory",
            self.memory,
            "--pids-limit",
            str(self.pids_limit),
            "--network",
            self.network,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
        ]


DEFAULT_LIMITS = SandboxLimits()
