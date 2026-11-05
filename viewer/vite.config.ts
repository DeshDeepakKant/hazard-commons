import { defineConfig } from "vite";

// Dev: proxy the FastAPI server (server/app.py, default :8765). Override with HC_API.
// Build: fully static; replay mode works with no backend.
const api = process.env.HC_API ?? "http://127.0.0.1:8765";

export default defineConfig({
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": api,
      "/world": api,
      "/ws": { target: api.replace(/^http/, "ws"), ws: true },
    },
  },
  build: { target: "es2022", chunkSizeWarningLimit: 1500 },
});
