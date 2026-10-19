import { useCallback, useEffect, useState } from "react";
import { op, openDocument, uploadDocument } from "../api";
import { notify } from "../store";

// Materials and tools (`wbench bom`): what the project needs, what the owner has, what is still to
// order or make. "Have" is the owner's own record (`wbench have`, ledger/inventory.jsonl); editing
// it here calls the same operation with the page's owner credential. Items not ready to order say
// so ("on hold", "decide first", "optional") and stay out of the shopping list. A product page pasted
// in an item's link field goes to the agent to check (`wbench check-link`); one it found right
// becomes the item's source, and the shopping list says where to buy. "In cart" marks an item as put
// in a shop's cart (`wbench cart`) until it is cleared. Documents (datasheets, orders, receipts)
// dropped on an item or in the Documents view are copied into the project's documents/ folder
// (`wbench doc-add`, R-0260) and listed under the items they belong to.

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
    carts?: Cart[];
    documents?: string[];
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

interface Cart {
    shop: string;
    url?: string;
    qty?: number;
    at: string;
}

interface Doc {
    path: string;
    kind: string;
    items: string[];
    title?: string;
    note?: string;
    size?: number;
    at?: string;
    missing?: boolean;
}

const DOC_KINDS = ["datasheet", "order", "receipt", "manual", "other"];

function docName(path: string): string {
    return path.split("/").pop() ?? path;
}

function errorText(e: unknown): string {
    return e instanceof Error ? e.message : String(e);
}

function open(path: string) {
    openDocument(path).catch((e) => notify("error", `${docName(path)}: ${errorText(e)}`));
}

interface Bom {
    title: string;
    items: Row[];
    documents?: Doc[];
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

type Filter = "all" | "short" | "tools" | "documents";

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
        .map((r) =>
            [r.short, r.unit, r.name, r.notes, r.source?.shop, r.source?.price, r.source?.currency, r.source?.url, (r.carts ?? []).map((c) => c.shop).join(", ")]
                .map(cell)
                .join("\t"),
        );
    return [["Qty", "Unit", "Item", "Notes", "Shop", "Price", "Currency", "Link", "In cart"].join("\t"), ...lines].join("\n");
}

const STATE_TEXT = { requested: "being checked", ok: "checked, ok", check: "check this", wrong: "not this one" };

function CartMarks({ row, shops, onSaved }: { row: Row; shops: string[]; onSaved: () => void }) {
    const [shop, setShop] = useState<string | null>(null); // null: the field is closed
    const save = async (args: Record<string, unknown>) => {
        try {
            await op("cart", { id: row.id, ...args });
            onSaved();
        } catch (e) {
            notify("error", `${row.name}: ${errorText(e)}`);
        }
    };
    const add = async () => {
        const name = (shop ?? "").trim();
        if (!name) return setShop(null);
        const url = row.source && row.source.shop?.toLowerCase() === name.toLowerCase() ? row.source.url : undefined;
        await save({ shop: name, ...(url ? { url } : {}), ...(row.short > 0 ? { qty: row.short } : {}) });
        setShop(null);
    };
    return (
        <div className="carts" data-carts={row.id}>
            {(row.carts ?? []).map((c) => (
                <span key={c.shop} className="cart" title={`put in the cart ${c.at.slice(0, 10)}`}>
                    in cart: {c.url ? <a href={c.url} target="_blank" rel="noreferrer">{c.shop}</a> : c.shop}
                    {c.qty !== undefined && ` ×${c.qty}`}
                    <button className="clear" title="Clear the mark (bought, or taken out of the cart)" onClick={() => save({ shop: c.shop, clear: true })}>
                        ×
                    </button>
                </span>
            ))}
            {shop === null ? (
                <button className="link add-cart" onClick={() => setShop(row.source?.shop ?? "")}>
                    + in cart
                </button>
            ) : (
                <input
                    className="cart-shop"
                    autoFocus
                    list="wb-shops"
                    placeholder="shop"
                    value={shop}
                    onChange={(e) => setShop(e.target.value)}
                    onBlur={add}
                    onKeyDown={(e) => {
                        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                        if (e.key === "Escape") setShop(null);
                    }}
                />
            )}
            {shops.length > 0 && shop !== null && (
                <datalist id="wb-shops">
                    {shops.map((s) => (
                        <option key={s} value={s} />
                    ))}
                </datalist>
            )}
        </div>
    );
}

// A file dropped on an item's row: which kind it is, then it is uploaded for that item.
function DroppedFile({ file, row, onDone }: { file: File; row: Row; onDone: (saved: boolean) => void }) {
    const [busy, setBusy] = useState(false);
    const send = async (kind: string) => {
        setBusy(true);
        try {
            const r = await uploadDocument(file, kind, [row.id]);
            notify("info", `${row.name}: ${r.record.path} saved`);
            onDone(true);
        } catch (e) {
            notify("error", `${file.name}: ${errorText(e)}`);
            onDone(false);
        }
    };
    return (
        <div className="dropped">
            <span>{file.name} is a</span>
            {DOC_KINDS.map((k) => (
                <button key={k} disabled={busy} onClick={() => send(k)}>
                    {k}
                </button>
            ))}
            <button className="link" disabled={busy} onClick={() => onDone(false)}>
                cancel
            </button>
        </div>
    );
}

function DocumentsView({ docs, rows, onSaved }: { docs: Doc[]; rows: Row[]; onSaved: () => void }) {
    const [kind, setKind] = useState("datasheet");
    const [items, setItems] = useState("");
    const [title, setTitle] = useState("");
    const [over, setOver] = useState(false);
    const [removing, setRemoving] = useState<string | null>(null);
    const names = Object.fromEntries(rows.map((r) => [r.id, r.name]));
    const upload = async (files: FileList | null) => {
        const ids = items.split(",").map((i) => i.trim()).filter(Boolean);
        for (const file of Array.from(files ?? [])) {
            try {
                const r = await uploadDocument(file, kind, ids, title.trim());
                notify("info", `${r.record.path} saved`);
            } catch (e) {
                notify("error", `${file.name}: ${errorText(e)}`);
            }
        }
        setTitle("");
        onSaved();
    };
    const remove = async (path: string) => {
        if (removing !== path) return setRemoving(path); // first click arms, second removes
        setRemoving(null);
        try {
            await op("doc-remove", { path });
            onSaved();
        } catch (e) {
            notify("error", `${docName(path)}: ${errorText(e)}`);
        }
    };
    const byKind = DOC_KINDS.map((k) => [k, docs.filter((d) => d.kind === k)] as const).filter(([, list]) => list.length);
    return (
        <div className="documents">
            <div
                className={over ? "dropzone over" : "dropzone"}
                onDragOver={(e) => {
                    e.preventDefault();
                    setOver(true);
                }}
                onDragLeave={() => setOver(false)}
                onDrop={(e) => {
                    e.preventDefault();
                    setOver(false);
                    upload(e.dataTransfer.files);
                }}
            >
                <label>
                    Kind{" "}
                    <select value={kind} onChange={(e) => setKind(e.target.value)}>
                        {DOC_KINDS.map((k) => (
                            <option key={k}>{k}</option>
                        ))}
                    </select>
                </label>
                <label>
                    Items <input list="wb-items" placeholder="ids, comma-separated" value={items} onChange={(e) => setItems(e.target.value)} />
                </label>
                <label>
                    Title <input placeholder="e.g. order number" value={title} onChange={(e) => setTitle(e.target.value)} />
                </label>
                <label className="pick">
                    Drop files here or <span className="link">choose</span>
                    <input type="file" multiple hidden onChange={(e) => upload(e.target.files).then(() => (e.target.value = ""))} />
                </label>
                <datalist id="wb-items">
                    {rows.map((r) => (
                        <option key={r.id} value={r.id}>
                            {r.name}
                        </option>
                    ))}
                </datalist>
            </div>
            {byKind.length === 0 && <p className="dim">No documents yet.</p>}
            {byKind.map(([k, list]) => (
                <section key={k}>
                    <h3>{k === "other" ? "Other" : `${k[0].toUpperCase()}${k.slice(1)}s`}</h3>
                    <table className="docs">
                        <tbody>
                            {list.map((d) => (
                                <tr key={d.path} data-doc={d.path}>
                                    <td>
                                        <button className="link" disabled={d.missing} onClick={() => open(d.path)} title={d.path}>
                                            {d.title || docName(d.path)}
                                        </button>
                                        {d.missing && <span className="error"> file missing</span>}
                                        {d.note && <div className="dim">{d.note}</div>}
                                    </td>
                                    <td className="dim">{d.items.map((i) => names[i] ?? i).join(", ")}</td>
                                    <td className="dim">{d.at?.slice(0, 10)}</td>
                                    <td>
                                        <button className={removing === d.path ? "danger" : "link"} onClick={() => remove(d.path)} onBlur={() => setRemoving(null)}>
                                            {removing === d.path ? "really delete?" : "delete"}
                                        </button>
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
    const docs = bom.documents ?? [];
    const docOf = Object.fromEntries(docs.map((d) => [d.path, d]));
    const shops = [...new Set(bom.items.flatMap((r) => [r.source?.shop, ...(r.carts ?? []).map((c) => c.shop)]).filter((s): s is string => !!s))].sort();
    const inCart = bom.items.filter((r) => r.carts?.length && r.short > 0).length;
    const groups = [...new Set(rows.map((r) => r.group))];
    const counts = Object.entries(bom.short).filter(([, n]) => n > 0);
    return (
        <div className="materials">
            <div className="materials-bar">
                {(["short", "all", "tools", "documents"] as Filter[]).map((f) => (
                    <button key={f} className={f === filter ? "tab active" : "tab"} data-filter={f} onClick={() => setFilter(f)}>
                        {f === "short" ? "Still needed" : f === "all" ? "Everything" : f === "tools" ? "Tools" : `Documents${docs.length ? ` (${docs.length})` : ""}`}
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
                {counts.length ? `${counts.map(([a, n]) => `${n} ${a}`).join(", ")}${inCart ? `; ${inCart} already in a cart` : ""}.` : "Nothing is short."}{" "}
                Type how many you have; it is saved when you leave the field. The shopping list holds only what is ready to order. Paste a
                product page under Where and the agent checks it against the item; one that checks out is kept as the item's source.
                Drop a datasheet, order or receipt on an item to keep it with it.
            </p>
            {(bom.problems ?? []).map((p) => (
                <p key={p} className="error">
                    {p}
                </p>
            ))}
            {filter === "documents" && <DocumentsView docs={docs} rows={bom.items} onSaved={load} />}
            {filter !== "documents" && groups.map((group) => (
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
                                    <ItemRow key={r.id} row={r} docOf={docOf} shops={shops} onSaved={load} />
                                ))}
                        </tbody>
                    </table>
                </section>
            ))}
        </div>
    );
}

function ItemRow({ row: r, docOf, shops, onSaved }: { row: Row; docOf: Record<string, Doc>; shops: string[]; onSaved: () => void }) {
    const [dropped, setDropped] = useState<File | null>(null);
    const [over, setOver] = useState(false);
    return (
        <tr
            data-item={r.id}
            className={[r.short > 0 ? "short" : "ok", over ? "drop-over" : ""].join(" ").trim()}
            onDragOver={(e) => {
                if (!e.dataTransfer.types.includes("Files")) return;
                e.preventDefault();
                setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => {
                e.preventDefault();
                setOver(false);
                if (e.dataTransfer.files[0]) setDropped(e.dataTransfer.files[0]);
            }}
        >
            <td title={[r.notes, r.used_for && `for: ${r.used_for}`, r.service && `instead: ${r.service}`].filter(Boolean).join("\n")}>
                {r.name}
                {r.used_for && <div className="dim">{r.used_for}</div>}
                {r.files?.map((f) => (
                    <div key={f.name} className={f.held.length ? "hold" : "dim"} title={f.held.join("\n")}>
                        {f.name}: {f.held.length ? "held" : f.state}
                    </div>
                ))}
                {(r.documents ?? []).map((p) => (
                    <button key={p} className="link doc" title={p} onClick={() => open(p)}>
                        {docOf[p]?.kind ?? "document"}: {docOf[p]?.title || docName(p)}
                    </button>
                ))}
                {dropped && <DroppedFile file={dropped} row={r} onDone={(saved) => (setDropped(null), saved && onSaved())} />}
            </td>
            <td>{r.qty_text || `${r.qty}${r.unit ? ` ${r.unit}` : ""}`}</td>
            <td>
                <HaveInput row={r} onSaved={onSaved} />
            </td>
            <td className={NOT_READY.includes(r.action) && r.short > 0 ? "hold" : "dim"} title={r.hold || r.status || ""}>
                {r.short > 0 ? `${r.action} ${r.short}` : "ok"}
                {r.short > 0 && r.hold && <div className="dim">{r.hold}</div>}
            </td>
            <td>
                <LinkCell row={r} onSaved={onSaved} />
                {(r.kind === "bought" || r.kind === "tools") && (r.short > 0 || !!r.carts?.length) && <CartMarks row={r} shops={shops} onSaved={onSaved} />}
            </td>
        </tr>
    );
}
