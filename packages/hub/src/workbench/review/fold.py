"""An item's state as a fold over its events.

Whose move it is (`status`):

    opened by the owner                         waiting_agent
    opened by a reviewer: finding, comment,     waiting_agent  (the author agent answers a review)
      change
    opened by the agent, or a reviewer's        waiting_owner
      decision or measurement
    reply                                       its `status` if given, else waiting_agent after the
                                                owner or a reviewer, waiting_owner after the agent
    answer                                      waiting_agent, also when questions stay open: the
                                                agent acts on what was answered and hands back
    status                                      its `to`
    delivered, attach, link                     no change

Questions: an answer may cover some of them; the others stay open. A later answer to the same
question replaces the earlier one. Approved values merge in event order, later values win.

Unread for the agent: owner events after the last `delivered`. Unread for the owner: agent and
reviewer events after the owner's last event (there is no read receipt for the owner; acting on
the item is taken as having read it). Last activity ignores `delivered`.

The fold also checks what the schema alone cannot: one `open` and it comes first, answers only
on decisions and measurements, question and option ids that exist, text only where the question
allows free text.
"""

from __future__ import annotations

from workbench.errors import InvalidInput


def initial_status(kind: str, role: str) -> str:
    if kind == "job":
        return "running"  # until the runner reports the end
    if role == "owner":
        return "waiting_agent"
    if role == "reviewer" and kind in ("finding", "comment", "change"):
        return "waiting_agent"
    return "waiting_owner"


def _after_reply(role: str) -> str:
    return "waiting_owner" if role == "agent" else "waiting_agent"


def fold(events: list[dict], item_id: str) -> dict:
    """The state of item `item_id` from its validated events (raises InvalidInput if inconsistent)."""
    if not events:
        raise InvalidInput(f"{item_id}: no events")
    first = events[0]
    if first["event"] != "open":
        raise InvalidInput(f"{item_id}: the first event must be open, not {first['event']}")
    kind = first["kind"]
    questions = first.get("questions", [])
    by_qid = {q["id"]: q for q in questions}
    status = initial_status(kind, first["by"]["role"])
    answers: dict[str, dict] = {}
    approved: dict[str, object] = {}
    attachments: list[str] = []
    links: list[str] = []
    last_delivered = -1
    delivered = None

    for i, ev in enumerate(events[1:], start=1):
        where = f"{item_id} event {i} ({ev['event']})"
        etype = ev["event"]
        if etype == "open":
            raise InvalidInput(f"{where}: an item has exactly one open event, the first")
        if etype == "reply":
            status = ev.get("status") or _after_reply(ev["by"]["role"])
        elif etype == "answer":
            if kind not in ("decision", "measurement"):
                raise InvalidInput(f"{where}: a {kind} takes no answers; reply instead")
            for qid, answer in ev.get("answers", {}).items():
                question = by_qid.get(qid)
                if question is None:
                    known = f"questions: {', '.join(by_qid)}" if by_qid else f"{item_id} has no questions"
                    raise InvalidInput(f"{where}: no question {qid!r}; {known}")
                option_ids = [o["id"] for o in question["options"]]
                if "choice" in answer and answer["choice"] not in option_ids:
                    raise InvalidInput(
                        f"{where}: {answer['choice']!r} is not an option of {qid}; options: {option_ids}"
                    )
                if "text" in answer and not question.get("free_text", False):
                    raise InvalidInput(
                        f"{where}: question {qid} does not take free text; choose an option, put remarks in the body"
                    )
                answers[qid] = {**answer, "at": ev["at"], "event": i}
            approved.update(ev.get("approves", {}))
            status = "waiting_agent"
        elif etype == "status":
            status = ev["to"]
        elif etype == "delivered":
            last_delivered = i
            delivered = {"at": ev["at"], **{k: ev[k] for k in ("to", "via") if k in ev}}
        elif etype == "attach":
            if ev["path"] in attachments:
                raise InvalidInput(f"{where}: {ev['path']} is already attached")
            attachments.append(ev["path"])
        elif etype == "link":
            links.append(ev["to"])

    def role(ev: dict) -> str | None:
        return ev.get("by", {}).get("role")

    owner_events = [i for i, ev in enumerate(events) if role(ev) == "owner"]
    # A job's end, reported by the runner, is news for both sides: the agent is woken for it like
    # for an owner event, and the owner sees it in the page.
    runner_events = [i for i, ev in enumerate(events) if role(ev) == "runner"]
    last_owner = owner_events[-1] if owner_events else -1
    activity = [(i, ev) for i, ev in enumerate(events) if ev["event"] != "delivered"]
    last_i, last_ev = activity[-1]

    state = {
        "id": item_id,
        "kind": kind,
        "title": first["title"],
        "status": status,
        "opened": {"at": first["at"], "by": first["by"], "build": first["build"]},
        "targets": first.get("targets", []),
        "body": first.get("body", ""),
    }
    if kind == "decision":
        state["corrections"] = first.get("corrections", [])
        state["evidence"] = first.get("evidence", [])
        state["questions"] = questions
    if kind == "measurement":
        state["quantity"] = first["quantity"]
        state["unit"] = first["unit"]
    state.update(
        {
            "answers": answers,
            "open_questions": [q["id"] for q in questions if q["id"] not in answers],
            "approved": approved,
            "attachments": attachments,
            "links": links,
            "last_activity": {"at": last_ev["at"], "role": role(last_ev), "event": last_ev["event"], "index": last_i},
            "unread_agent": [i for i in sorted(owner_events + runner_events) if i > last_delivered],
            "unread_owner": [
                i for i, ev in enumerate(events) if i > last_owner and role(ev) in ("agent", "reviewer", "runner")
            ],
            "delivered": delivered,
            "event_count": len(events),
        }
    )
    return state
