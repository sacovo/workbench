import { useEffect, useState, type ReactNode } from "react";
import { assetJson, assetUrl, op } from "../api";
import { notify, useStore, WHOSE, type ItemState, type Question, type Target } from "../store";
import * as viewer from "../viewer/host";

/* eslint-disable @typescript-eslint/no-explicit-any */
type Any = any;

function who(by: { role: string; name?: string } | undefined): string {
    return by ? by.role + (by.name ? ` (${by.name})` : "") : "wbench";
}

function targetText(t: Target): string {
    if (t.kind === "part") return `part ${t.label}` + (t.assembly ? ` in ${t.assembly}` : "");
    if (t.kind === "file") return `file ${t.path}` + (t.line ? `:${t.line}` : "");
    if (t.kind === "view") return `view ${t.id}`;
    return `${t.kind} ${t.key}`;
}

function effectText(effect: unknown): string | null {
    if (typeof effect === "string") return effect;
    if (effect && typeof effect === "object") return Object.entries(effect).map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join(", ");
    return null;
}

function Attachment({ path }: { path: string }) {
    const [url, setUrl] = useState<string | null>(null);
    const png = path.endsWith(".png");
    const evidence = path.endsWith("/evidence.json");
    useEffect(() => {
        if (!png) return;
        let gone = false;
        assetUrl(path).then((u) => !gone && setUrl(u)).catch(() => undefined);
        return () => {
            gone = true;
        };
    }, [path]);
    async function lookFromHere() {
        try {
            const data = await assetJson<Any>(path);
            if (data?.camera && viewer.getViewer()) viewer.applyView({ camera: data.camera });
        } catch (e) {
            notify("error", String((e as Error).message));
        }
    }
    return (
        <div className="attachment">
            {png && url ? <img src={url} alt={path} /> : <span className="dim">{path.split("/").pop()}</span>}
            {evidence && (
                <button className="link" onClick={lookFromHere} title="apply the camera the comment was made with">
                    look from the comment's camera
                </button>
            )}
        </div>
    );
}

function Decision({ item }: { item: Any }) {
    const [choices, setChoices] = useState<Record<string, string>>({});
    const [texts, setTexts] = useState<Record<string, string>>({});
    const [remark, setRemark] = useState("");
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        setChoices({});
        setTexts({});
        setRemark("");
    }, [item.id]);
    const questions: Question[] = item.questions ?? [];

    async function submit() {
        const answers: Record<string, { choice?: string; text?: string }> = {};
        for (const q of questions) {
            const a: { choice?: string; text?: string } = {};
            if (choices[q.id]) a.choice = choices[q.id];
            if (texts[q.id]?.trim()) a.text = texts[q.id].trim();
            if (a.choice || a.text) answers[q.id] = a;
        }
        if (!Object.keys(answers).length) {
            notify("error", "Choose an option (or write an answer) for at least one question.");
            return;
        }
        setBusy(true);
        try {
            await op("answer", { id: item.id, answers, ...(remark.trim() ? { body: remark.trim() } : {}) });
            setChoices({});
            setTexts({});
            setRemark("");
            notify("info", `Answer to ${item.id} recorded.`);
        } catch (e) {
            notify("error", String((e as Error).message));
        } finally {
            setBusy(false);
        }
    }

    return (
        <div className="decision">
            {questions.map((q) => {
                const answered = item.answers[q.id];
                const label = (id?: string) => q.options.find((o) => o.id === id)?.label ?? id;
                return (
                    <div className="question" key={q.id} data-question={q.id}>
                        <p>
                            <b>{q.id}</b> {q.text}{" "}
                            {answered ? (
                                <span className="answered">
                                    answered: {label(answered.choice)}
                                    {answered.text ? ` "${answered.text}"` : ""}
                                </span>
                            ) : (
                                <span className="open">open</span>
                            )}
                        </p>
                        <div className="options">
                            {q.options.map((o) => (
                                <button
                                    key={o.id}
                                    data-option={o.id}
                                    className={`option${choices[q.id] === o.id ? " chosen" : ""}${q.recommended === o.id ? " recommended" : ""}`}
                                    onClick={() => setChoices({ ...choices, [q.id]: choices[q.id] === o.id ? "" : o.id })}
                                    title={effectText(o.effect) ?? undefined}
                                >
                                    {o.label}
                                    {q.recommended === o.id && <span className="rec">recommended</span>}
                                    {effectText(o.effect) && <span className="effect">{effectText(o.effect)}</span>}
                                </button>
                            ))}
                        </div>
                        {q.free_text && (
                            <textarea
                                data-free-text={q.id}
                                placeholder="your answer"
                                value={texts[q.id] ?? ""}
                                onChange={(e) => setTexts({ ...texts, [q.id]: e.target.value })}
                            />
                        )}
                    </div>
                );
            })}
            <textarea placeholder="remarks with the answer (optional)" value={remark} onChange={(e) => setRemark(e.target.value)} />
            <button className="primary" data-action="submit-answer" onClick={submit} disabled={busy}>
                Submit answers
            </button>
            <span className="dim"> Unanswered questions stay open.</span>
        </div>
    );
}

function Event({ ev, questions }: { ev: Any; questions: Question[] }) {
    if (ev.event === "delivered") return <li className="event dim">delivered to the agent {ev.at}</li>;
    const head = `${who(ev.by)} · ${ev.at}`;
    let what: ReactNode = ev.event;
    if (ev.event === "open") what = `opened (${ev.kind})`;
    if (ev.event === "status") what = `set status ${ev.to}`;
    if (ev.event === "attach") what = `attached ${ev.path.split("/").pop()}`;
    if (ev.event === "link") what = `linked ${ev.to}`;
    if (ev.event === "reply") what = "replied" + (ev.status ? `, status ${ev.status}` : "");
    if (ev.event === "answer") {
        what = (
            <>
                answered
                <ul>
                    {Object.entries(ev.answers ?? {}).map(([qid, a]: [string, Any]) => {
                        const q = questions.find((x) => x.id === qid);
                        const option = q?.options.find((o) => o.id === a.choice);
                        return (
                            <li key={qid}>
                                {qid}: {option?.label ?? a.choice ?? ""} {a.text ? `"${a.text}"` : ""}
                            </li>
                        );
                    })}
                </ul>
            </>
        );
    }
    return (
        <li className={`event ${ev.by?.role ?? ""}`}>
            <div className="dim">{head}</div>
            <div>{what}</div>
            {ev.body && ev.event !== "open" && <div className="body">{ev.body}</div>}
        </li>
    );
}

export function ItemPanel() {
    const { item: id, review } = useStore();
    const summary: ItemState | undefined = review.items.find((i) => i.id === id);
    const [data, setData] = useState<Any>(null);
    const [reply, setReply] = useState("");
    useEffect(() => {
        if (!id) return;
        let gone = false;
        op("item", { id, events: true })
            .then((d) => !gone && setData(d))
            .catch((e) => notify("error", String(e.message)));
        return () => {
            gone = true;
        };
    }, [id, summary?.event_count]);
    if (!id) return <p className="dim">Choose an item in the inbox.</p>;
    if (!data || data.id !== id) return <p className="dim">Loading {id}…</p>;

    async function send(extra: Record<string, unknown> = {}) {
        try {
            if (reply.trim()) await op("reply", { id, body: reply.trim(), role: "owner", ...extra });
            else if (extra.status) await op("set-status", { id, to: extra.status, role: "owner" });
            setReply("");
        } catch (e) {
            notify("error", String((e as Error).message));
        }
    }

    return (
        <div className="item" data-item-view={id}>
            <h2>
                {id} {data.title}
            </h2>
            <div className="meta">
                {data.kind} · <b>{WHOSE[data.status as ItemState["status"]]}</b> · opened by {who(data.opened.by)} {data.opened.at} · build {data.opened.build ?? "none"}
            </div>
            {(data.corrections ?? []).length > 0 && (
                <div className="corrections">
                    {data.corrections.map((c: string, i: number) => (
                        <p key={i}>
                            <b>Correction:</b> {c}
                        </p>
                    ))}
                </div>
            )}
            {data.body && <div className="body">{data.body}</div>}
            {[...data.targets, ...(data.evidence ?? [])].length > 0 && (
                <ul className="targets">
                    {data.targets.map((t: Target, i: number) => (
                        <li key={`t${i}`}>target: {targetText(t)}</li>
                    ))}
                    {(data.evidence ?? []).map((t: Target, i: number) => (
                        <li key={`e${i}`}>evidence: {targetText(t)}</li>
                    ))}
                </ul>
            )}
            {data.attachments.length > 0 && (
                <div className="attachments">
                    {data.attachments.map((p: string) => (
                        <Attachment key={p} path={p} />
                    ))}
                </div>
            )}
            {Object.keys(data.approved ?? {}).length > 0 && (
                <div className="approved">approved: {effectText(data.approved)}</div>
            )}
            {data.kind === "decision" && <Decision item={data} />}
            <h3>Thread</h3>
            <ul className="thread">
                {data.events.map((ev: Any) => (
                    <Event key={ev.index} ev={ev} questions={data.questions ?? []} />
                ))}
            </ul>
            <textarea placeholder="reply to the agent" value={reply} onChange={(e) => setReply(e.target.value)} />
            <button onClick={() => send()} disabled={!reply.trim()}>
                Reply
            </button>
            <button onClick={() => send({ status: "done" })}>{reply.trim() ? "Reply and close" : "Close as done"}</button>
        </div>
    );
}
