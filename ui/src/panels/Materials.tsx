import { useCallback, useEffect, useState } from "react";
import { op } from "../api";
import { notify } from "../store";

// Materials and tools (`wbench bom`): what the project needs, what the owner has, what is still to
// order or make. "Have" is the owner's own record (`wbench have`, ledger/inventory.jsonl); editing
// it here calls the same operation with the page's owner credential. Items not ready to order say
// so ("on hold", "decide first", "optional") and stay out of the shopping list.

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
    have: number;
    short: number;
    action: string;
    recorded: boolean;
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

function shoppingList(rows: Row[]): string {
    return rows
        .filter((r) => r.short > 0 && r.kind === "bought" && r.action === "to order")
        .map((r) => `${r.short} x ${r.name}${r.notes ? ` (${r.notes})` : ""}`)
        .join("\n");
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
                when you leave the field. The shopping list holds only what is ready to order.
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
                                    </tr>
                                ))}
                        </tbody>
                    </table>
                </section>
            ))}
        </div>
    );
}
