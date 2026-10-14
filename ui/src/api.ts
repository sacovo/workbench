// The hub's HTTP API. The token comes from the URL fragment `wbench open` puts it in (#token=...),
// never sent to the server as part of a URL, and is kept in sessionStorage so a reload works.

export const token: string | null = (() => {
    const m = location.hash.match(/token=([^&]+)/);
    if (m) {
        const t = decodeURIComponent(m[1]);
        try {
            sessionStorage.setItem("wb-token", t);
        } catch {
            /* storage off: the token lives as long as this document */
        }
        history.replaceState(null, "", location.pathname + location.search);
        return t;
    }
    try {
        return sessionStorage.getItem("wb-token");
    } catch {
        return null;
    }
})();

export class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
        super(message);
        this.status = status;
    }
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (token) headers.set("X-Workbench-Token", token);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...init, headers });
    const json = (response.headers.get("Content-Type") ?? "").includes("json");
    const body = json ? await response.json() : await response.text();
    if (!response.ok) {
        const message = json && body && typeof body === "object" && "error" in body ? String((body as { error: unknown }).error) : String(body);
        throw new ApiError(response.status, message);
    }
    return body as T;
}

/** One registry operation, as `wbench <name>` runs it (POST /api/op/<name>). */
export function op<T = Record<string, unknown>>(name: string, args: Record<string, unknown> = {}): Promise<T> {
    return api<T>(`/api/op/${name}`, { method: "POST", body: JSON.stringify(args) });
}

export function blob<T = unknown>(sha: string): Promise<T> {
    return api<T>(`/api/blob/${sha}`);
}

/** An attachment (review/assets/<item>/<name>) as an object URL for <img>. */
export async function assetUrl(path: string): Promise<string> {
    const [, , item, name] = path.split("/");
    const response = await fetch(`/api/asset/${item}/${name}`, { headers: token ? { "X-Workbench-Token": token } : {} });
    if (!response.ok) throw new ApiError(response.status, await response.text());
    return URL.createObjectURL(await response.blob());
}

export async function assetJson<T = unknown>(path: string): Promise<T> {
    const [, , item, name] = path.split("/");
    return api<T>(`/api/asset/${item}/${name}`);
}
