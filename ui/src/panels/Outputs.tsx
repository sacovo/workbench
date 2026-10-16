import { useCallback, useEffect, useState } from "react";
import { op } from "../api";

// Declared outputs (`wbench outputs`, workbench.outputs): each manufacturing file the project makes,
// whether it exists, whether it was stamped as made by `wbench make`, whether it still matches the
// code's build id (the fingerprint), and whether the ledger holds it until a value is measured.

interface Stamp {
    at: string;
    build: string | null;
    job: string | null;
    sha256: string;
    values?: Record<string, { value: unknown; unit: string; state: string; item: string }>;
}

interface Output {
    name: string;
    path: string;
    command: string;
    needs: string[];
    held: string[];
    state: "missing" | "unstamped" | "modified" | "stale" | "current";
    bytes: number | null;
    stamp: Stamp | null;
}

const MEANING: Record<Output["state"], string> = {
    missing: "not made yet",
    unstamped: "a file nobody stamped: made outside `wbench make`, or before stamps",
    modified: "changed since it was stamped",
    stale: "made at an older build: the design changed since",
    current: "made from the code as it is now",
};

export function OutputsPanel() {
    const [data, setData] = useState<{ build: string | null; outputs: Output[] } | null>(null);
    const [error, setError] = useState<string | null>(null);
    const load = useCallback(() => {
        op<{ build: string | null; outputs: Output[] }>("outputs")
            .then((d) => {
                setData(d);
                setError(null);
            })
            .catch((e) => setError(e instanceof Error ? e.message : String(e)));
    }, []);
    useEffect(load, [load]);
    useEffect(() => {
        window.addEventListener("focus", load);
        return () => window.removeEventListener("focus", load);
    }, [load]);
    if (error) return <p className="error">No outputs: {error}</p>;
    if (!data) return <p className="dim">Loading the outputs…</p>;
    if (!data.outputs.length) return <p className="dim">The project declares no outputs.</p>;
    return (
        <div className="outputs">
            <p className="dim">
                Build <code>{data.build ?? "unknown"}</code>. A file is made with <code>wbench make NAME</code>, which stamps it with this build and
                the values it rests on.
            </p>
            <table className="bom">
                <thead>
                    <tr>
                        <th>File</th>
                        <th>State</th>
                        <th>Made</th>
                    </tr>
                </thead>
                <tbody>
                    {data.outputs.map((o) => (
                        <tr key={o.name} data-output={o.name}>
                            <td title={o.command}>
                                {o.path}
                                {o.held.map((h) => (
                                    <div key={h} className="hold">
                                        held: {h}
                                    </div>
                                ))}
                            </td>
                            <td className={`out-${o.state}`} title={MEANING[o.state]}>
                                {o.state}
                            </td>
                            <td className="dim">
                                {o.stamp ? (
                                    <span title={Object.entries(o.stamp.values ?? {}).map(([k, v]) => `${k} = ${String(v.value)} ${v.unit} (${v.state})`).join("\n")}>
                                        {o.stamp.at.slice(0, 16).replace("T", " ")} by {o.stamp.job ?? "?"}
                                    </span>
                                ) : (
                                    "-"
                                )}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}
