import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// The built page is committed into the hub package, so `wbench serve` needs no Node at runtime.
const outDir = fileURLToPath(new URL("../packages/hub/src/workbench/hub/static", import.meta.url));

export default defineConfig({
    base: "/",
    oxc: { jsx: { runtime: "automatic" } },
    build: {
        outDir,
        emptyOutDir: true,
        target: "es2022",
        sourcemap: false,
        // three-cad-viewer is one large module; splitting it buys nothing for a local page
        chunkSizeWarningLimit: 8000,
    },
});
