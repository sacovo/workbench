"""Errors a caller can act on.

Every operation raises a `WorkbenchError` subclass for anything the owner, the agent or the UI
did or can fix (bad input, unknown item, no project, hub not running). Its message is the whole
report: the CLI prints it on stderr and exits non-zero, the hub will answer with `http_status`
and `{"error": message}`. Anything else that escapes an operation is a bug.
"""

from __future__ import annotations


class WorkbenchError(Exception):
    http_status = 400


class InvalidInput(WorkbenchError):
    """Arguments or file contents that do not match the schema."""

    http_status = 400


class NotFound(WorkbenchError):
    http_status = 404


class Conflict(WorkbenchError):
    """The request is well formed but contradicts the current state."""

    http_status = 409


class ProjectError(WorkbenchError):
    """No workbench project here, or its [tool.workbench] table is wrong."""

    http_status = 400


class GitError(WorkbenchError):
    """Not a git repository, no commit yet, or a git command failed."""

    http_status = 500


class Busy(WorkbenchError):
    """The project's lock stayed held by another process for too long."""

    http_status = 503


class HubNotRunning(WorkbenchError):
    http_status = 503
