import { useEffect, useState } from "react";
import { api } from "../api";
import { notify, openItem, useStore } from "../store";
import * as viewer from "../viewer/host";

function fmt(v: unknown): string {
    if (typeof v === "number") return Number(v.toPrecision(6)).toString();
    if (Array.isArray(v)) return `[${v.map(fmt).join(", ")}]`;
    if (v && typeof v === "object") return Object.entries(v).map(([k, x]) => `${k} ${fmt(x)}`).join(", ");
    return String(v);
}

const SHOWN = ["geom_type", "area", "length", "radius", "volume", "normal", "centre", "point"];

export function CommentPanel() {
    const { selection } = useStore();
    const [title, setTitle] = useState(() => (selection ? `Comment on ${selection.label}` : ""));
    const [body, setBody] = useState("");
    const [busy, setBusy] = useState(false);
    useEffect(() => {
        if (selection) setTitle(`Comment on ${selection.label}`);
    }, [selection?.seq]);

    if (!selection) {
        return (
            <div className="comment">
                <p>Nothing picked yet.</p>
                <p className="dim">
                    Pick with the Properties measure tool in the viewer's toolbar (click a face, an edge or a vertex), or double-click a part
                    with no tool active.
                </p>
            </div>
        );
    }

    async function submit() {
        if (!selection || !title.trim()) return;
        setBusy(true);
        try {
            const image = await viewer.thumbnail();
            const item = await api<{ id: string }>("/api/page/comment", {
                method: "POST",
                body: JSON.stringify({ seq: selection.seq, title: title.trim(), body, camera: viewer.cameraState(), image }),
            });
            setBody("");
            notify("info", `${item.id} sent to the agent.`);
            openItem(item.id);
        } catch (e) {
            notify("error", String((e as Error).message));
        } finally {
            setBusy(false);
        }
    }

    return (
        <div className="comment" data-pick={selection.seq}>
            <h3>
                <span data-pick-label>{selection.label}</span> <span className="dim">in {selection.assembly}</span>
            </h3>
            <p>
                <span data-pick-kind>{selection.kind}</span> picked by {selection.via}
                {selection.point && <span className="dim"> at {fmt(selection.point)} mm</span>}
            </p>
            <table className="props">
                <tbody>
                    {SHOWN.filter((k) => k in selection.world).map((k) => (
                        <tr key={k}>
                            <td>{k}</td>
                            <td>{fmt(selection.world[k])}</td>
                        </tr>
                    ))}
                </tbody>
            </table>
            <p className="dim">The comment targets the part. Its point, the camera, the build and a picture of the view are frozen with it.</p>
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="title" />
            <textarea value={body} onChange={(e) => setBody(e.target.value)} placeholder="what should the agent know?" />
            <button className="primary" data-action="send-comment" onClick={submit} disabled={busy || !title.trim()}>
                Send comment
            </button>
        </div>
    );
}
