import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// Build the SPA into the Flask package so it's served as static files and the
// Pi never needs Node. The built dist/ is committed; rebuild when the UI changes.
export default defineConfig({
  plugins: [svelte()],
  base: "/",
  build: {
    outDir: "../src/sim_monitor/web/spa",
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` proxies the JSON API to a locally-running --simulate app.
    proxy: { "/api": "http://127.0.0.1:8080" },
  },
});
