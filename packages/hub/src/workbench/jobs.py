"""Jobs: long commands the workbench runs for someone and reports on when they end.

`wbench run TITLE --command '...'` opens a review item of kind `job` (status `running`) and starts
this module as a detached process:

    python -m workbench.jobs --project ROOT ITEM_ID

It runs the command with bash in the project root (without a foreign VIRTUAL_ENV), writes its
output to .workbench/jobs/<item id>.log, and when the command ends appends a reply by the role
`runner` with the exit code, the time taken and the end of the log, making the item the agent's
move. A runner event is news for both sides (review/fold.py): the agent link delivers it to a
working agent through its hooks and wakes an idle one through herdr, exactly as for an owner's
answer, and the owner sees it in the page. A command started with a plain `&` in a shell tells
nobody when it ends; this is the way to run one that must be followed up.

The runner reports even when the command cannot start or the runner itself fails; a runner that
is killed (the machine goes down) leaves the item running, which `wbench job-check` turns into a
report.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

from workbench.project import Project, find_project
from workbench.review.store import ReviewStore, make_by

TAIL_LINES = 30
TAIL_CHARS = 4000
# Variables a job must not inherit: a foreign virtual environment (uv would use it) and the
# stale NVIDIA settings (see workbench.hub.kernel).
_DROP_ENV = ("VIRTUAL_ENV", "__GLX_VENDOR_LIBRARY_NAME", "LIBVA_DRIVER_NAME", "XWAYLAND_NO_GLAMOR")


def jobs_dir(project: Project) -> Path:
    return project.state_dir / "jobs"


def spec_path(project: Project, item_id: str) -> Path:
    return jobs_dir(project) / f"{item_id}.json"


def log_path(project: Project, item_id: str) -> Path:
    return jobs_dir(project) / f"{item_id}.log"


def start(project: Project, item_id: str, command: str) -> dict:
    """Write the job's spec and start its runner, detached from the caller (own session, no
    terminal), so it outlives the shell or agent turn that started it."""
    project.ensure_state_dir()
    directory = jobs_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    spec = {"item": item_id, "command": command, "cwd": str(project.root), "started": time.time()}
    spec_path(project, item_id).write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    runner = subprocess.Popen(
        [sys.executable, "-m", "workbench.jobs", "--project", str(project.root), item_id],
        cwd=project.root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=open(directory / f"{item_id}.runner.log", "ab"),
        start_new_session=True,
    )
    spec["runner_pid"] = runner.pid
    spec_path(project, item_id).write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
    return {"log": str(log_path(project, item_id).relative_to(project.root)), "runner_pid": runner.pid}


def _tail(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    lines = text.rstrip("\n").splitlines()[-TAIL_LINES:]
    return "\n".join(lines)[-TAIL_CHARS:]


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min {secs} s" if minutes else f"{secs} s"


def report(project: Project, item_id: str, headline: str, log: Path | None) -> dict:
    """Append the runner's report; the item becomes the agent's move."""
    body = headline
    tail = _tail(log) if log else ""
    if tail:
        body += f"\n\nLast lines of {log.relative_to(project.root)}:\n\n```\n{tail}\n```"
    fields = {"by": make_by("runner"), "body": body, "status": "waiting_agent"}
    return ReviewStore(project).append(item_id, "reply", fields)


def run(project: Project, item_id: str) -> int:
    spec = json.loads(spec_path(project, item_id).read_text(encoding="utf-8"))
    log = log_path(project, item_id)
    began = time.time()
    try:
        with log.open("ab") as out:
            process = subprocess.Popen(["bash", "-lc", spec["command"]], cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
            spec["pid"] = process.pid
            spec_path(project, item_id).write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
            code = process.wait()
    except Exception as exc:  # noqa: BLE001 - reported as the job's end
        report(project, item_id, f"The job could not run: {type(exc).__name__}: {exc}", log)
        return 1
    took = _duration(time.time() - began)
    if code < 0:
        headline = f"Killed by signal {signal.Signals(-code).name} after {took}."
    else:
        headline = f"{'Finished' if code == 0 else 'Failed'}: exit {code} after {took}."
    report(project, item_id, headline, log)
    return 0


def check(project: Project) -> list[str]:
    """Report every job that is still running but whose runner is gone (killed, machine restarted)."""
    reported = []
    store = ReviewStore(project)
    states, _ = store.states()
    for state in states:
        if state["kind"] != "job" or state["status"] != "running":
            continue
        try:
            spec = json.loads(spec_path(project, state["id"]).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            spec = {}
        pid = spec.get("runner_pid")
        if pid and _alive(pid):
            continue
        report(project, state["id"], "The runner is gone (killed, or the machine restarted) and the job's end was never seen.", log_path(project, state["id"]))
        reported.append(state["id"])
    return reported


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m workbench.jobs", description="Run one job and report its end.")
    parser.add_argument("--project", required=True)
    parser.add_argument("item")
    args = parser.parse_args(argv)
    project = find_project(args.project)
    try:
        return run(project, args.item)
    except Exception:  # noqa: BLE001 - last resort: never end silently
        traceback.print_exc()
        try:
            report(project, args.item, "The runner failed; see .workbench/jobs/" + args.item + ".runner.log.", None)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
