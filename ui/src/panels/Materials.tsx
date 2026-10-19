import { useCallback, useEffect, useState } from "react";
import { op } from "../api";
import { notify } from "../store";

// Materials and tools (`wbench bom`): what the project needs, what the owner has, what is still to
// order or make. "Have" is the owner's own record (`wbench have`, ledger/inventory.jsonl); editing
// it here calls the same operation with the page's owner credential. Items not ready to order say
// so ("on hold", "decide first", "optional") and stay out of the shopping list. A product page pasted
// in an item's link field goes to the agent to check (`wbench check-link`); one it found right
// becomes the item's source, and the shopping list says where to buy.

interface Row {
    id: string;
    name: string;
    group: string;
    kind: string;
    qty: number;
    qty_text?: string;
    status?: string;
    notes?: string;
    used_for?: string;
    service?: string;
    unit?: string;
    hold?: string;
    files?: { name: string; state: string; held: string[] }[];
    link?: Link;
    source?: Link;
    have: number;
    short: number;
    action: string;
    recorded: boolean;
}

interface Link {
    url: string;
    state: "requested" | "ok" | "check" | "wrong";
    summary?: string;
    price?: number;
    currency?: string;
    shop?: string;
    note?: string;
}

interface Bom {
    title: string;
    items: Row[];
    short: Record<string, number>;
    problems?: string[];
}

// What the owner typed, as an amount the item takes: digits with an optional decimal part (comma
// or point), whole unless the item has a unit. Anything else, an empty field included, is null.
export function parseHave(text: string, unit?: string): number | null {
    const t = text.trim().replace(",", ".");
    if (!/^\d+(\.\d+)?$/.test(t)) return null;
    const qty = Number(t);
    if (!Number.isFinite(qty) || (!unit && !Number.isInteger(qty))) return null;
    return qty;
}

const NOT_READY = ["on hold", "decide first", "optional"];

type Filter = "all" | "short" | "tools";

function HaveInput({ row, onSaved }: { row: Row; onSaved: () => void }) {
    const [value, setValue] = useState(String(row.have));
    useEffect(() => setValue(String(row.have)), [row.have]);
    const save = async () => {
        if (value === String(row.have)) return; // unchanged: tabbing through writes nothing
        const qty = parseHave(value, row.unit);
        if (qty === null) {
            setValue(String(row.have));
            return notify("error", `${row.name}: ${row.unit ? `a number of ${row.unit}` : "a whole number"}, at least 0`);
        }
        if (qty === row.have && row.recorded) return;
        try {
            await op("have", { id: row.id, qty });
            onSaved();
        } catch (e) {
            notify("error", `${row.name}: ${e instanceof Error ? e.message : String(e)}`);
            setValue(String(row.have));
        }
    };
    return (
        <input
            className="have"
            data-have={row.id}
            value={value}
            inputMode="decimal"
            onChange={(e) => setValue(e.target.value)}
            onBlur={save}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
        />
    );
}

function linkText(l: Link): string {
    return [l.shop, l.price !== undefined ? `${l.price} ${l.currency ?? ""}`.trim() : ""].filter(Boolean).join(", ") || new URL(l.url).hostname;
}

// Tab-separated with a header row, so it pastes into a spreadsheet one field per column; commas in
// names and notes stay inside their cell. Tabs and line breaks inside a field become spaces.
function shoppingList(rows: Row[]): string {
    const cell = (v: unknown) => (v === undefined || v === null ? "" : String(v).replace(/[\t\r\n]+/g, " ").trim());
    const lines = rows
        .filter((r) => r.short > 0 && r.kind === "bought" && r.action === "to order")
        .map((r) => [r.short, r.unit, r.name, r.notes, r.source?.shop, r.source?.price, r.source?.currency, r.source?.url].map(cell).join("\t"));
    return [["Qty", "Unit", "Item", "Notes", "Shop", "Price", "Currency", "Link"].join("\t"), ...lines].join("\n");
}

const STATE_TEXT = { requested: "being checked", ok: "checked, ok", check: "check this", wrong: "not this one" };

function LinkCell({ row, onSaved }: { row: Row; onSaved: () => void }) {
    const [url, setUrl] = useState("");
    const [busy, setBusy] = useState(false);
    const pending = row.link && row.link.state !== "ok" ? row.link : undefined; // an ok one is the source
    const send = async () => {
        if (!url.trim()) return;
        setBusy(true);
        try {
            await op("check-link", { id: row.id, url: url.trim() });
            setUrl("");
            notify("info", `${row.name}: sent to the agent to check`);
            onSaved();
        } catch (e) {
            notify("error", `${row.name}: ${e instanceof Error ? e.message : String(e)}`);
        } finally {
            setBusy(false);
        }
    };
    return (
        <div className="link-cell" data-link={row.id}>
            {row.source && (
                <a href={row.source.url} target="_blank" rel="noreferrer" title={row.source.summary ?? ""} className="source">
                    {linkText(row.source)}
                </a>
            )}
            {pending && (
                <div className={pending.state === "requested" ? "dim" : "hold"} title={pending.summary ?? pending.url}>
                    <a href={pending.url} target="_blank" rel="noreferrer">
                        {STATE_TEXT[pending.state]}
                    </a>
                    {pending.summary && <span>: {pending.summary}</span>}
                </div>
            )}
            <input
                className="link"
                placeholder="product page"
                value={url}
                disabled={busy}
                onChange={(e) => setUrl(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && send()}
            />
            {url.trim() && (
                <button className="check" disabled={busy} onClick={send}>
                    Check
                </button>
            )}
        </div>
    );
}

export function MaterialsPanel() {
    const [bom, setBom] = useState<Bom | null>(null);
    const [error, setError] = useState<string | null>(null);
    const [filter, setFilter] = useState<Filter>("short");
    const load = useCallback(() => {
        op<Bom>("bom")
            .then((b) => {
                setBom(b);
                setError(null);
            })
            .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    }, []);
    useEffect(load, [load]);
    useEffect(() => {
        // The inventory may change elsewhere (`wbench have`, another window): reload on return.
        window.addEventListener("focus", load);
        return () => window.removeEventListener("focus", load);
    }, [load]);
    if (error) return <p className="error">No materials list: {error}</p>;
    if (!bom) return <p className="dim">Loading the materials list…</p>;
    const rows = bom.items.filter((r) => (filter === "tools" ? r.kind === "tools" : filter === "short" ? r.short > 0 : true));
    const groups = [...new Set(rows.map((r) => r.group))];
    const counts = Object.entries(bom.short).filter(([, n]) => n > 0);
    return (
        <div className="materials">
            <div className="materials-bar">
                {(["short", "all", "tools"] as Filter[]).map((f) => (
                    <button key={f} className={f === filter ? "tab active" : "tab"} data-filter={f} onClick={() => setFilter(f)}>
                        {f === "short" ? "Still needed" : f === "all" ? "Everything" : "Tools"}
                    </button>
                ))}
                <button
                    className="link"
                    onClick={() => navigator.clipboard.writeText(shoppingList(bom.items)).then(() => notify("info", "Shopping list copied"))}
                >
                    Copy shopping list
                </button>
            </div>
            <p className="dim">
                {counts.length ? `${counts.map(([a, n]) => `${n} ${a}`).join(", ")}.` : "Nothing is short."} Type how many you have; it is saved
                when you leave the field. The shopping list holds only what is ready to order. Paste a product page under Where and the
                agent checks it against the item; one that checks out is kept as the item's source.
            </p>
            {(bom.problems ?? []).map((p) => (
                <p key={p} className="error">
                    {p}
                </p>
            ))}
            {groups.map((group) => (
                <section key={group}>
                    <h3>{group}</h3>
                    <table className="bom">
                        <thead>
                            <tr>
                                <th>Item</th>
                                <th>Need</th>
                                <th>Have</th>
                                <th />
                                <th>Where</th>
                            </tr>
                        </thead>
                        <tbody>
                            {rows
                                .filter((r) => r.group === group)
                                .map((r) => (
                                    <tr key={r.id} data-item={r.id} className={r.short > 0 ? "short" : "ok"}>
                                        <td title={[r.notes, r.used_for && `for: ${r.used_for}`, r.service && `instead: ${r.service}`].filter(Boolean).join("\n")}>
                                            {r.name}
                                            {r.used_for && <div className="dim">{r.used_for}</div>}
                                            {r.files?.map((f) => (
                                                <div key={f.name} className={f.held.length ? "hold" : "dim"} title={f.held.join("\n")}>
                                                    {f.name}: {f.held.length ? "held" : f.state}
                                                </div>
                                            ))}
                                        </td>
                                        <td>{r.qty_text || `${r.qty}${r.unit ? ` ${r.unit}` : ""}`}</td>
                                        <td>
                                            <HaveInput row={r} onSaved={load} />
                                        </td>
                                        <td className={NOT_READY.includes(r.action) && r.short > 0 ? "hold" : "dim"} title={r.hold || r.status || ""}>
                                            {r.short > 0 ? `${r.action} ${r.short}` : "ok"}
                                            {r.short > 0 && r.hold && <div className="dim">{r.hold}</div>}
                                        </td>
                                        <td>
                                            <LinkCell row={r} onSaved={load} />
                                        </td>
                                    </tr>
                                ))}
                        </tbody>
                    </table>
                </section>
            ))}
        </div>
    );
}
