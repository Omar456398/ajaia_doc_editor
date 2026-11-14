import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "node:path";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, path.resolve(__dirname, ".."), "");
  const proxyTarget = env.VITE_BACKEND_PROXY_TARGET || "http://127.0.0.1:8000";

  const apiProxy = {
    // Proxy relative /api HTTP and WebSocket requests to FastAPI
    // so frontend always calls same-origin /api (URL-agnostic)
    "/api": {
      target: proxyTarget,
      changeOrigin: true,
      ws: true,
    },
  };

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: "0.0.0.0",
      port: 5173,
      allowedHosts: true,
      proxy: apiProxy,
    },
    preview: {
      host: "0.0.0.0",
      port: 4173,
      allowedHosts: true,
      proxy: apiProxy,
    },
  };
});
