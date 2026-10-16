// The hub's HTTP API. Two secrets, kept in sessionStorage so a reload works. The URL fragment
// `wbench open` / `serve --open` puts on the window (#token=...&pair=..., never sent to the server
// as part of a URL) carries the token and a one-time pairing code, which the page trades for the
// page credential (POST /api/page/pair) before its first call: the credential itself never sits on
// Chromium's command line (R-0117). Tests that start a hub with a fixed credential may still pass
// it as #pc=... directly. `token` is the ordinary hub.json token, sent on every call.
// `pageCredential` is the page credential: only a window the hub itself opened ever receives it
// (never hub.json, a log or an op response), and the hub requires it, in addition to `token`, for
// a call that writes as the owner (answer, comment, or role: "owner" on reply/set-status/view-
// create/...) - the guard against the agent's own shell or an HTTP client with just the hub.json
// token writing as the owner. Sent on every call regardless, same as `token`: the hub alone
// decides which calls need it.

interface PageSecrets {
    token: string | null;
    pageCredential: string | null;
}

function readPageSecrets(): PageSecrets {
    const fromUrl = new URLSearchParams(location.hash.slice(1));
    const urlToken = fromUrl.get("token");
    const urlPageCredential = fromUrl.get("pc");
    pairingCode = fromUrl.get("pair");
    if (urlToken || urlPageCredential || pairingCode) {
        try {
            if (urlToken) sessionStorage.setItem("wb-token", urlToken);
            if (urlPageCredential) sessionStorage.setItem("wb-page-credential", urlPageCredential);
        } catch {
            /* storage off: the secrets live as long as this document */
        }
        history.replaceState(null, "", location.pathname + location.search);
    }
    const stored = (key: string): string | null => {
        try {
            return sessionStorage.getItem(key);
        } catch {
            return null;
        }
    };
    return {
        token: urlToken ?? stored("wb-token"),
        pageCredential: urlPageCredential ?? stored("wb-page-credential"),
    };
}

let pairingCode: string | null = null;
const secrets = readPageSecrets();
export const token = secrets.token;
let pageCredential = secrets.pageCredential;

// Trades the window's pairing code for the page credential, once; every call waits for it.
const paired: Promise<void> = pairingCode
    ? fetch("/api/page/pair", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(token ? { "X-Workbench-Token": token } : {}) },
          body: JSON.stringify({ code: pairingCode }),
      })
          .then((r) => (r.ok ? r.json() : null))
          .then((body) => {
              if (body && typeof body.pc === "string") {
                  pageCredential = body.pc;
                  try {
                      sessionStorage.setItem("wb-page-credential", body.pc);
                  } catch {
                      /* storage off: the credential lives as long as this document */
                  }
              }
          })
          .catch(() => undefined)
    : Promise.resolve();

export class ApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
        super(message);
        this.status = status;
    }
}

export async function api<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
    await paired;
    const headers = new Headers(init.headers);
    if (token) headers.set("X-Workbench-Token", token);
    if (pageCredential) headers.set("X-Workbench-Page-Credential", pageCredential);
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
