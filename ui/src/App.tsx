import { useEffect, useRef } from "react";
import { mountViewer, refreshPins } from "./hub";
import { CommentPanel } from "./panels/Comment";
import { InboxPanel } from "./panels/Inbox";
import { ItemPanel } from "./panels/Item";
import { ViewsPanel } from "./panels/Views";
import { setStore, useStore, type Tab } from "./store";
import { Banner, TopBar } from "./TopBar";

const TABS: { id: Tab; label: string }[] = [
    { id: "inbox", label: "Inbox" },
    { id: "item", label: "Item" },
    { id: "comment", label: "Comment" },
    { id: "views", label: "Views" },
];

function ViewerPane() {
    const ref = useRef<HTMLDivElement>(null);
    const { settings } = useStore();
    useEffect(() => {
        if (ref.current && settings) mountViewer(ref.current);
    }, [settings]);
    return <div className="viewer" ref={ref} />;
}

function SidePanel() {
    const { tab, review, item } = useStore();
    useEffect(() => refreshPins(), [item]); // the open item's pin is drawn highlighted
    const unread = review.items.filter((i) => i.unread_owner.length > 0 && i.status !== "done" && i.status !== "dropped").length;
    return (
        <aside className="side">
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
            </div>
        </aside>
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
