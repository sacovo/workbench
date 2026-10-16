// The page's state: what the hub pushed (state, review, pick) and what the owner is looking at.
// One immutable object; components read it with useStore() and re-render when it is replaced.

import { useSyncExternalStore } from "react";

export interface KernelInfo {
    state: "starting" | "ready" | "building" | "crashed";
    error: { kind: string; message: string; traceback?: string; detail?: string; generation?: number; at?: string } | null;
    activity: { assembly: string; args: Record<string, unknown>; generation: number; new_kernel: boolean; since: string } | null;
    generation: number | null;
    pid: number | null;
}

export interface Arg {
    name: string;
    default: unknown;
    annotation: string | null;
}

export interface HubState {
    project: { name: string; root: string };
    build: string | null;
    kernel: KernelInfo;
    manifest: { name: string; assemblies: Record<string, { doc: string | null; args: Arg[] }> } | null;
    scene: { scene: string; assembly: string; args: Record<string, unknown>; build: string | null; generation: number; updates: number; stale: boolean } | null;
    stale: boolean;
}

export interface Option {
    id: string;
    label: string;
    effect?: unknown;
}

export interface Question {
    id: string;
    text: string;
    options: Option[];
    recommended?: string;
    free_text?: boolean;
}

export interface Target {
    kind: string;
    label?: string;
    assembly?: string;
    key?: string;
    path?: string;
    line?: number;
    id?: string;
}

export interface ItemState {
    id: string;
    kind: string;
    title: string;
    status: "waiting_owner" | "waiting_agent" | "done" | "dropped";
    opened: { at: string; by: { role: string; name?: string }; build: string | null };
    targets: Target[];
    body: string;
    corrections?: string[];
    evidence?: Target[];
    questions?: Question[];
    answers: Record<string, { choice?: string; text?: string; at: string }>;
    open_questions: string[];
    approved: Record<string, unknown>;
    attachments: string[];
    links: string[];
    last_activity: { at: string; role: string | null; event: string };
    unread_owner: number[];
    unread_agent: number[];
    event_count: number;
    quantity?: string;
    unit?: string;
}

export interface SavedView {
    id: string;
    assembly: string;
    camera: { position: number[]; target: number[]; quaternion?: number[]; zoom?: number };
    hidden?: string[];
    section?: { planes: { normal: number[]; offset: number }[]; intersection?: boolean };
    note?: string;
    by: { role: string; name?: string };
    at: string;
    build: string | null;
}

export interface Pin {
    item: string;
    assembly: string | null;
    point: number[];
    normal: number[] | null;
    status: string;
}

export interface Review {
    items: ItemState[];
    views: SavedView[];
    pins: Pin[];
    unreadable: { file: string; error: string }[];
}

export interface Selection {
    seq: number;
    assembly: string | null;
    scene: string;
    build: string | null;
    label: string;
    kind: string;
    geom_type: string | null;
    world: Record<string, unknown>;
    point: number[] | null;
    normal: number[] | null;
    via: string;
    at: string;
}

export interface Notice {
    id: number;
    level: "info" | "error";
    message: string;
}

export type Tab = "inbox" | "item" | "comment" | "views" | "materials";

export interface Store {
    connected: boolean;
    settings: Record<string, unknown> | null;
    state: HubState | null;
    review: Review;
    selection: Selection | null;
    shown: { scene: string; assembly: string } | null;
    tab: Tab;
    item: string | null;
    notices: Notice[];
    renderer: string | null;
}

let store: Store = {
    connected: false,
    settings: null,
    state: null,
    review: { items: [], views: [], pins: [], unreadable: [] },
    selection: null,
    shown: null,
    tab: "inbox",
    item: null,
    notices: [],
    renderer: null,
};
const listeners = new Set<() => void>();

export function getStore(): Store {
    return store;
}

export function setStore(patch: Partial<Store>) {
    store = { ...store, ...patch };
    for (const listener of listeners) listener();
}

export function useStore(): Store {
    return useSyncExternalStore(
        (listener) => {
            listeners.add(listener);
            return () => listeners.delete(listener);
        },
        () => store,
    );
}

let noticeId = 0;

export function notify(level: Notice["level"], message: string) {
    const notice = { id: ++noticeId, level, message };
    setStore({ notices: [...store.notices, notice].slice(-5) });
    setTimeout(() => setStore({ notices: store.notices.filter((n) => n.id !== notice.id) }), level === "error" ? 12000 : 5000);
}

export function openItem(id: string) {
    setStore({ tab: "item", item: id });
}

export const WHOSE: Record<ItemState["status"], string> = {
    waiting_owner: "Your move",
    waiting_agent: "Agent's move",
    done: "Done",
    dropped: "Dropped",
};
