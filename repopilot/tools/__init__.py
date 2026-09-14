"""The agent's tool surface: a host-side workspace plus six typed tools (spec §6)."""

from repopilot.tools.paths import PathError, is_test_path, resolve_repo_path
from repopilot.tools.results import ToolError, ToolResult
from repopilot.tools.toolbox import TOOL_SPECS, TestSandbox, Toolbox
from repopilot.tools.workspace import Workspace, WorkspaceError

__all__ = [
    "TOOL_SPECS",
    "PathError",
    "TestSandbox",
    "ToolError",
    "ToolResult",
    "Toolbox",
    "Workspace",
    "WorkspaceError",
    "is_test_path",
    "resolve_repo_path",
]
