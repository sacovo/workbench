import { useEffect, useState } from "react";
import { op } from "../api";
import { getStore, notify, useStore, type SavedView } from "../store";
import * as viewer from "../viewer/host";

let pending: SavedView | null = null; // a view of another assembly, applied once that one is shown

function apply(view: SavedView) {
    viewer.applyView({ camera: view.camera as viewer.Camera, hidden: view.hidden ?? [], section: view.section ? { planes: view.section.planes, intersection: !!view.section.intersection } : null });
}

async function open(id: string) {
    try {
        const view = await op<SavedView>("view-get", { id });
        if (getStore().shown?.assembly === view.assembly) {
            apply(view);
        } else {
            pending = view;
            notify("info", `Showing ${view.assembly} for ${id}.`);
            await op("show", { assembly: view.assembly });
        }
    } catch (e) {
        notify("error", String((e as Error).message));
    }
}

export function ViewsPanel() {
    const { review, shown } = useStore();
    const [note, setNote] = useState("");
    useEffect(() => {
        if (pending && shown?.assembly === pending.assembly) {
            const view = pending;
            pending = null;
            setTimeout(() => apply(view), 0);
        }
    }, [shown?.scene]);

    async function save() {
        if (!shown) return;
        try {
            const state = viewer.currentView();
            const created = await op<{ id: string }>("view-create", {
                assembly: shown.assembly,
                camera: state.camera,
                ...(state.hidden.length ? { hidden: state.hidden } : {}),
                ...(state.section ? { section: state.section } : {}),
                ...(note.trim() ? { note: note.trim() } : {}),
                role: "owner",
            });
            setNote("");
            notify("info", `Saved ${created.id}.`);
        } catch (e) {
            notify("error", String((e as Error).message));
        }
    }

    return (
        <div className="views">
            <div className="save">
                <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="note for this view (optional)" />
                <button onClick={save} disabled={!shown} data-action="save-view">
                    Save view
                </button>
            </div>
            {review.views.length === 0 && <p className="dim">No saved views.</p>}
            <ul>
                {[...review.views].reverse().map((v) => (
                    <li key={v.id} className="row" data-view={v.id} onClick={() => open(v.id)} title="apply its camera, hidden parts and section">
                        <span className="id">{v.id}</span>
                        <span className="kind">{v.assembly}</span>
                        <span className="title">{v.note?.split("\n")[0] || <span className="dim">no note</span>}</span>
                        <span className="dim">
                            {v.by.role} {v.at.slice(0, 16).replace("T", " ")}
                            {v.hidden?.length ? ` · ${v.hidden.length} hidden` : ""}
                            {v.section ? " · section" : ""}
                        </span>
                    </li>
                ))}
            </ul>
        </div>
    );
}
