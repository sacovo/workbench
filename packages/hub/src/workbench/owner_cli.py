"""`wbench owner`: the owner's own terminal, a plain question-and-answer loop.

It lists what waits on the owner (decisions, measurements, comments to read) and lets them answer:
pick an option (Enter takes the recommended one), type their own text where a question allows it,
give a measured value, reply, or close an item; and walk through the materials and tools to record
what they have. Everything goes through the same operations as the page (answer, reply,
set-status, have), as the owner. Like those operations, it refuses to run in a Claude Code agent's
shell.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from workbench.errors import WorkbenchError
from workbench.ops import default_registry
from workbench.ops.bom_ops import owner_items
from workbench.project import find_project


class NotForAgents(WorkbenchError):
    pass


def _call(project, name: str, **arguments):
    return default_registry().get(name).call(arguments, project=project)


def run(directory: str | None = None, ask: Callable[[str], str] = input, say: Callable[[str], None] = print) -> int:
    if os.environ.get("CLAUDECODE"):
        raise NotForAgents("`wbench owner` is the owner's terminal; an agent's shell cannot use it")
    project = find_project(directory)
    while True:
        items = owner_items(project)
        say("")
        say(f"{len(items)} item{'s' if len(items) != 1 else ''} wait on you:" if items else "Nothing waits on you.")
        for number, state in enumerate(items, 1):
            say(f"  {number}. {state['id']} {state['kind']}: {state['title']}")
        say("  m. materials and tools: record what you have")
        say("  q. quit")
        choice = ask("> ").strip().lower()
        if choice in ("q", "quit", ""):
            return 0
        if choice == "m":
            materials(project, ask, say)
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(items):
            item(project, items[int(choice) - 1], ask, say)
            continue
        say("Type a number, m or q.")


def item(project, state: dict, ask, say) -> None:
    say("")
    say(f"{state['id']} {state['kind']}: {state['title']}")
    for line in (state.get("corrections") or []):
        say(f"  correction: {line}")
    if state.get("body"):
        say("")
        say(state["body"])
    if state["kind"] == "decision":
        answers = {}
        questions = {q["id"]: q for q in state.get("questions", [])}
        for qid in state.get("open_questions", []):
            question = questions[qid]
            say("")
            say(f"{qid}: {question['text']}")
            for option in question["options"]:
                rec = "  (recommended, Enter)" if question.get("recommended") == option["id"] else ""
                say(f"   {option['id']}) {option['label']}{rec}")
            if question.get("free_text"):
                say("   or type your own answer")
            say("   s) skip this question")
            while True:
                reply = ask(f"{qid}> ").strip()
                ids = [o["id"] for o in question["options"]]
                if not reply and question.get("recommended"):
                    answers[qid] = {"choice": question["recommended"]}
                elif reply in ids:
                    answers[qid] = {"choice": reply}
                elif reply.lower() == "s":
                    pass
                elif reply and question.get("free_text"):
                    answers[qid] = {"text": reply}
                else:
                    say(f"   one of {', '.join(ids)}" + (", your own text" if question.get("free_text") else "") + " or s")
                    continue
                break
        if answers:
            remark = ask("remark (Enter for none)> ").strip()
            _call(project, "answer", id=state["id"], answers=answers, body=remark)
            say(f"answered {', '.join(answers)} on {state['id']}")
        return
    if state["kind"] == "measurement":
        say(f"measure {state['quantity']} in {state['unit']} (Enter to leave it open)")
        while True:
            reply = ask(f"value in {state['unit']}> ").strip().replace(",", ".")
            if not reply:
                return
            try:
                value = float(reply)
            except ValueError:
                say("a number, please")
                continue
            break
        remark = ask("how measured (Enter for none)> ").strip()
        _call(project, "answer", id=state["id"], approves={state["quantity"]: value}, body=remark)
        say(f"recorded {value:g} {state['unit']} on {state['id']}")
        return
    reply = ask("reply (Enter: none; d: mark done)> ").strip()
    if reply.lower() == "d":
        _call(project, "set-status", id=state["id"], to="done", role="owner")
        say(f"{state['id']} done")
    elif reply:
        _call(project, "reply", id=state["id"], body=reply, role="owner")
        say(f"replied on {state['id']}")


def materials(project, ask, say) -> None:
    status = _call(project, "bom")
    say("")
    say("For each item, type how many you have (Enter keeps it, q stops).")
    for row in status["items"]:
        need = row.get("qty_text") or f"{row['qty']:g}"
        state = f"have {row['have']:g}" + ("" if row["recorded"] else " (not recorded)")
        reply = ask(f"{row['group']} | {row['name']} | need {need}, {state}> ").strip().replace(",", ".")
        if reply.lower() == "q":
            break
        if not reply:
            continue
        try:
            qty = float(reply)
        except ValueError:
            say("  a number, please; skipped")
            continue
        result = _call(project, "have", id=row["id"], qty=qty)
        if result["item"]["short"] > 0:
            say(f"  {result['item']['action']} {result['item']['short']:g}")
    short = _call(project, "bom", short=True)["items"]
    say("")
    say(f"Still short: {len(short)} item{'s' if len(short) != 1 else ''}; `wbench bom --short` lists them.")
