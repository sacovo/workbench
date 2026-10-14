// ocp-viewer-core ships plain JavaScript without declarations. Only what this page uses.
declare module "ocp-viewer-core" {
    export interface Page {
        showSplash(config: Record<string, unknown>): void;
        setTheme(next: string): void;
        handleMessage(message: unknown): void;
        resize(): void;
    }
    export function createPage(options: {
        Viewer: unknown;
        Display: unknown;
        Timer: unknown;
        send: (command: string, message: unknown) => void;
        overrides: { display: Record<string, unknown>; viewer: Record<string, unknown> };
        theme?: string;
        container?: string | HTMLElement;
        getSize?: () => { width: number; height: number };
        listen?: boolean;
    }): Page;
    export const VERSION: string;
}

// The stylesheet export ("./css") of three-cad-viewer, imported for its side effect.
declare module "three-cad-viewer/css";
