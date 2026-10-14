import { useEffect, useState } from "react";
import { op } from "./api";
import { send } from "./hub";
import { notify, useStore, type Arg } from "./store";

/** An argument's text as typed; empty means the builder's default. */
function parseArg(text: string, arg: Arg): unknown {
    const t = text.trim();
    if (t === "") return undefined;
    const annotation = arg.annotation ?? "";
    if (/\b(float|int)\b/.test(annotation) || typeof arg.default === "number") {
        const n = Number(t);
        if (!Number.isFinite(n)) throw new Error(`${arg.name}: ${t} is not a number`);
        return /\bint\b/.test(annotation) && !/\bfloat\b/.test(annotation) ? Math.round(n) : n;
    }
    if (/\bbool\b/.test(annotation) || typeof arg.default === "boolean") return t === "true";
    return t;
}

export function TopBar() {
    const { state, connected } = useStore();
    const assemblies = state?.manifest?.assemblies ?? {};
    const names = Object.keys(assemblies);
    const scene = state?.scene ?? null;
    const [assembly, setAssembly] = useState<string>("");
    const [values, setValues] = useState<Record<string, string>>({});
    const [busy, setBusy] = useState(false);

    // Follow what is shown: the selector and the arguments start from the current scene.
    useEffect(() => {
        if (!scene) return;
        setAssembly(scene.assembly);
        const next: Record<string, string> = {};
        for (const [k, v] of Object.entries(scene.args)) next[k] = v === null || v === undefined ? "" : String(v);
        setValues(next);
    }, [scene?.scene]);

    const current = assembly || names[0] || "";
    const args = assemblies[current]?.args ?? [];

    async function show() {
        try {
            const parsed: Record<string, unknown> = {};
            for (const arg of args) {
                const value = parseArg(values[arg.name] ?? "", arg);
                if (value !== undefined) parsed[arg.name] = value;
            }
            setBusy(true);
            await op("show", { assembly: current, args: parsed });
        } catch (e) {
            notify("error", String((e as Error).message ?? e));
        } finally {
            setBusy(false);
        }
    }

    const kernel = state?.kernel;
    return (
        <header className="topbar">
            <span className="project">{state?.project.name ?? "workbench"}</span>
            <span className="build" title="build id of the working tree">
                build {state?.build ?? "none"}
                {scene && scene.build !== state?.build && <span className="dim"> (scene {scene.build ?? "none"})</span>}
            </span>
            <span className={`kernel ${kernel?.state ?? "starting"}`} data-kernel-state={kernel?.state ?? ""}>
                kernel {kernel?.state ?? (connected ? "starting" : "offline")}
            </span>
            {scene?.stale && <span className="stale">stale</span>}
            <span className="spacer" />
            <select value={current} onChange={(e) => setAssembly(e.target.value)} aria-label="assembly">
                {names.map((n) => (
                    <option key={n} value={n}>
                        {n}
                    </option>
                ))}
            </select>
            {args.map((arg) => (
                <label key={arg.name} className="arg" title={assemblies[current]?.doc ?? ""}>
                    {arg.name}
                    <input
                        value={values[arg.name] ?? ""}
                        placeholder={arg.default === null ? "default" : String(arg.default)}
                        onChange={(e) => setValues({ ...values, [arg.name]: e.target.value })}
                        onKeyDown={(e) => e.key === "Enter" && show()}
                        size={7}
                    />
                </label>
            ))}
            <button onClick={show} disabled={busy || !current}>
                Show
            </button>
            <button onClick={() => send({ type: "rebuild" })} disabled={!scene} title="build again in a new kernel, picking up code changes">
                Rebuild
            </button>
        </header>
    );
}

export function Banner() {
    const { state, connected, renderer } = useStore();
    const [open, setOpen] = useState(false);
    if (!state) return connected ? null : <div className="banner error">Not connected to the hub.</div>;
    const k = state.kernel;
    const parts = [];
    if (renderer && /swiftshader|llvmpipe|softpipe/i.test(renderer)) {
        parts.push(
            <div key="g" className="banner error" data-banner="software-gl">
                Software rendering ({renderer}): the page is not on the GPU. Stale NVIDIA settings cause this; see <code>wbench doctor</code>.
            </div>,
        );
    }
    if (!connected) parts.push(<div key="c" className="banner error">Connection to the hub lost; reconnecting.</div>);
    if (k.state === "building" && k.activity) {
        parts.push(
            <div key="b" className="banner info" data-banner="building">
                Building {k.activity.assembly} {JSON.stringify(k.activity.args)} in {k.activity.new_kernel ? "a new" : "the"} kernel ({k.activity.generation})
                {state.scene ? "; the last scene stays on screen and answers picks." : "."}
            </div>,
        );
    } else if (k.state === "starting") {
        parts.push(<div key="s" className="banner info" data-banner="starting">Starting the kernel.</div>);
    }
    if (k.state === "crashed") {
        parts.push(
            <div key="x" className="banner error" data-banner="crashed">
                <b>Kernel crashed:</b> {k.error?.message}. The last scene stays on screen; Show or Rebuild starts a new kernel.
                {k.error?.detail && <button className="link" onClick={() => setOpen(!open)}>{open ? "hide" : "details"}</button>}
                {open && <pre>{k.error?.detail}</pre>}
            </div>,
        );
    } else if (k.error) {
        parts.push(
            <div key="e" className="banner error" data-banner="error">
                <b>{k.error.kind === "manifest" ? "Manifest failed." : "Last build failed."}</b> {k.error.message}
                {(k.error.traceback || k.error.detail) && <button className="link" onClick={() => setOpen(!open)}>{open ? "hide" : "traceback"}</button>}
                {open && <pre>{k.error.traceback || k.error.detail}</pre>}
            </div>,
        );
    }
    if (state.scene?.stale && k.state !== "building") {
        parts.push(
            <div key="t" className="banner info" data-banner="stale">
                The code changed since this scene was built. <button className="link" onClick={() => send({ type: "rebuild" })}>Rebuild</button>
            </div>,
        );
    }
    return <>{parts}</>;
}
