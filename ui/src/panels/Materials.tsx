import { useCallback, useEffect, useState } from "react";
import { op } from "../api";
import { notify } from "../store";

// Materials and tools (`wbench bom`): what the project needs, what the owner has, what is still to
// order or make. "Have" is the owner's own record (`wbench have`, ledger/inventory.jsonl); editing
// it here calls the same operation with the page's owner credential.

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
    have: number;
    short: number;
    action: string;
    recorded: boolean;
}

interface Bom {
    title: string;
    items: Row[];
    short: Record<string, number>;
}

type Filter = "all" | "short" | "tools";

function HaveInput({ row, onSaved }: { row: Row; onSaved: () => void }) {
    const [value, setValue] = useState(String(row.have));
    useEffect(() => setValue(String(row.have)), [row.have]);
    const save = async () => {
        const qty = Number(value.replace(",", "."));
        if (!Number.isFinite(qty) || qty < 0) {
            setValue(String(row.have));
            return notify("error", `${row.name}: a number of at least 0`);
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
        .filter((r) => r.short > 0 && r.kind === "bought")
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
            <p className="dim">{counts.length ? counts.map(([a, n]) => `${n} ${a}`).join(", ") : "Nothing is short."} Type how many you have; it is saved when you leave the field.</p>
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
                                        </td>
                                        <td>{r.qty_text || r.qty}</td>
                                        <td>
                                            <HaveInput row={r} onSaved={load} />
                                        </td>
                                        <td className="dim">{r.short > 0 ? `${r.action} ${r.short}` : "ok"}</td>
                                    </tr>
                                ))}
                        </tbody>
                    </table>
                </section>
            ))}
        </div>
    );
}
