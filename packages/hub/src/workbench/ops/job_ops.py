"""Jobs: `wbench run` and `wbench job-check` (workbench.jobs)."""

from __future__ import annotations

from typing import Optional

from workbench import jobs
from workbench.errors import InvalidInput
from workbench.ops.registry import op
from workbench.project import Project
from workbench.review.store import ReviewStore, compact, make_by


def _human_run(r: dict) -> str:
    return f"{r['id']} running: {r['title']}\nlog {r['log']}; its end is reported on {r['id']} (the agent is woken for it)"


@op(
    "run",
    summary="Run a long command as a job; its end is reported as a review item and wakes the agent",
    human=_human_run,
    args={
        "title": "one line: what the job is",
        "command": "the shell command, run with bash in the project root",
        "body": "markdown: why it runs, what to look at in its output",
        "session": "the starting agent's session (default $WB_SESSION), woken first when it ends",
    },
)
def run(project: Project, title: str, command: str, body: str = "", session: Optional[str] = None) -> dict:
    """Open a `job` item and start the command detached. When it ends, the runner appends its exit
    code, time and the end of its output, and the item becomes the agent's move: the hooks deliver
    that to a working agent and herdr wakes an idle one, as for an owner's answer. Use it for any
    command whose end must be followed up (test suites, studies), instead of `&`."""
    if not command.strip():
        raise InvalidInput("command must not be empty")
    text = f"`{command}`" + (f"\n\n{body}" if body else "")
    state = ReviewStore(project).open_item({"by": make_by("agent", session), "kind": "job", "title": title, **compact({"body": text})})
    started = jobs.start(project, state["id"], command)
    return {"id": state["id"], "title": title, **started}


@op("job-check", summary="Report jobs still marked running whose runner is gone")
def job_check(project: Project) -> dict:
    """A runner killed with its job (reboot, kill -9) never reports; this reports each such job,
    so it becomes the agent's move instead of looking busy forever."""
    return {"reported": jobs.check(project)}
