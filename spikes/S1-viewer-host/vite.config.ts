import { defineConfig } from "vite";

export default defineConfig({
    base: "./",
    build: {
        outDir: "dist",
        target: "es2022",
        sourcemap: true,
        // three-cad-viewer is one 3.6 MB module; splitting it buys nothing here
        chunkSizeWarningLimit: 8000,
    },
});
