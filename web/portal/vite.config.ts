import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    // The API and the portal are separate processes in dev. Proxying keeps
    // one origin so websockets and cookies behave the same as in production.
    proxy: {
      "/api": { target: "http://localhost:8000", ws: true },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
