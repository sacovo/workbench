import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { mountViewer, refreshPins } from "./hub";
import { CommentPanel } from "./panels/Comment";
import { InboxPanel } from "./panels/Inbox";
import { ItemPanel } from "./panels/Item";
import { MaterialsPanel } from "./panels/Materials";
import { OutputsPanel } from "./panels/Outputs";
import { ViewsPanel } from "./panels/Views";
import { setStore, useStore, type Tab } from "./store";
import { Banner, TopBar } from "./TopBar";

const TABS: { id: Tab; label: string }[] = [
    { id: "inbox", label: "Inbox" },
    { id: "item", label: "Item" },
    { id: "comment", label: "Comment" },
    { id: "views", label: "Views" },
    { id: "materials", label: "Materials" },
    { id: "outputs", label: "Outputs" },
];

function ViewerPane() {
    const ref = useRef<HTMLDivElement>(null);
    const { settings } = useStore();
    useEffect(() => {
        if (ref.current && settings) mountViewer(ref.current);
    }, [settings]);
    return <div className="viewer" ref={ref} />;
}

// The side panel's width, one per kind of tab: reading (inbox, item, comment, views) keeps the viewer
// large; tables (materials, outputs) take more. The owner drags the divider; each width is kept in
// this browser only, and double-click returns to the default.
type Width = "reading" | "table";
const WIDTH_OF: Record<Tab, Width> = { inbox: "reading", item: "reading", comment: "reading", views: "reading", materials: "table", outputs: "table" };
const SIDE_MIN = 360;
const VIEWER_MIN = 480;

function defaultWidth(kind: Width): number {
    const w = window.innerWidth;
    return kind === "reading" ? Math.min(720, Math.max(440, w * 0.32)) : Math.min(1400, Math.max(560, w * 0.55));
}

function clampWidth(px: number): number {
    return Math.round(Math.max(SIDE_MIN, Math.min(px, window.innerWidth - VIEWER_MIN)));
}

function storedWidth(kind: Width): number | null {
    try {
        const v = Number(localStorage.getItem(`wb.side.${kind}`));
        return Number.isFinite(v) && v > 0 ? v : null;
    } catch {
        return null;
    }
}

function storeWidth(kind: Width, px: number | null) {
    try {
        if (px === null) localStorage.removeItem(`wb.side.${kind}`);
        else localStorage.setItem(`wb.side.${kind}`, String(px));
    } catch {
        /* a page without storage keeps the default */
    }
}

function useSideWidth(kind: Width): [number, (px: number | null) => void] {
    const [widths, setWidths] = useState<Record<Width, number | null>>(() => ({ reading: storedWidth("reading"), table: storedWidth("table") }));
    const [, setWindowWidth] = useState(window.innerWidth);
    useEffect(() => {
        const onResize = () => setWindowWidth(window.innerWidth);
        window.addEventListener("resize", onResize);
        return () => window.removeEventListener("resize", onResize);
    }, []);
    const set = (px: number | null) => {
        const value = px === null ? null : clampWidth(px);
        storeWidth(kind, value);
        setWidths((w) => ({ ...w, [kind]: value }));
    };
    return [clampWidth(widths[kind] ?? defaultWidth(kind)), set];
}

function Divider({ width, setWidth }: { width: number; setWidth: (px: number | null) => void }) {
    const drag = useRef<{ x: number; width: number } | null>(null);
    function down(e: PointerEvent<HTMLDivElement>) {
        drag.current = { x: e.clientX, width };
        e.currentTarget.setPointerCapture(e.pointerId);
        document.body.classList.add("resizing");
    }
    function move(e: PointerEvent<HTMLDivElement>) {
        if (drag.current) setWidth(drag.current.width + drag.current.x - e.clientX);
    }
    function up() {
        drag.current = null;
        document.body.classList.remove("resizing");
    }
    function key(e: KeyboardEvent<HTMLDivElement>) {
        const step = e.shiftKey ? 80 : 20;
        if (e.key === "ArrowLeft") setWidth(width + step);
        else if (e.key === "ArrowRight") setWidth(width - step);
        else if (e.key === "Home") setWidth(null);
        else return;
        e.preventDefault();
    }
    return (
        <div
            className="divider"
            role="separator"
            aria-orientation="vertical"
            aria-label="side panel width"
            aria-valuenow={width}
            tabIndex={0}
            title="drag to resize; double-click for the default width"
            onPointerDown={down}
            onPointerMove={move}
            onPointerUp={up}
            onPointerCancel={up}
            onDoubleClick={() => setWidth(null)}
            onKeyDown={key}
        />
    );
}

function SidePanel() {
    const { tab, review, item } = useStore();
    useEffect(() => refreshPins(), [item]); // the open item's pin is drawn highlighted
    const [width, setWidth] = useSideWidth(WIDTH_OF[tab]);
    const unread = review.items.filter((i) => i.unread_owner.length > 0 && i.status !== "done" && i.status !== "dropped").length;
    return (
        <>
        <Divider width={width} setWidth={setWidth} />
        <aside className="side" style={{ width }} data-width={WIDTH_OF[tab]}>
            <nav className="tabs">
                {TABS.map((t) => (
                    <button key={t.id} className={t.id === tab ? "tab active" : "tab"} data-tab={t.id} onClick={() => setStore({ tab: t.id })}>
                        {t.label}
                        {t.id === "inbox" && unread > 0 && <span className="count">{unread}</span>}
                    </button>
                ))}
            </nav>
            <div className="panel">
                {tab === "inbox" && <InboxPanel />}
                {tab === "item" && <ItemPanel />}
                {tab === "comment" && <CommentPanel />}
                {tab === "views" && <ViewsPanel />}
                {tab === "materials" && <MaterialsPanel />}
                {tab === "outputs" && <OutputsPanel />}
            </div>
        </aside>
        </>
    );
}

function Notices() {
    const { notices } = useStore();
    return (
        <div className="notices">
            {notices.map((n) => (
                <div key={n.id} className={`notice ${n.level}`}>
                    {n.message}
                </div>
            ))}
        </div>
    );
}

export function App() {
    return (
        <div className="app">
            <TopBar />
            <Banner />
            <div className="main">
                <ViewerPane />
                <SidePanel />
            </div>
            <Notices />
        </div>
    );
}
