import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 构建产物直接放进 Python 包，由 examprep serve 提供（不入库）
const OUT_DIR = "../packages/app/src/examprep/web";
const API = process.env.EXAMPREP_API ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  build: { outDir: OUT_DIR, emptyOutDir: true },
  server: {
    port: 5173,
    // 开发时把接口转给 examprep serve；SSE 不缓冲
    proxy: { "/api": { target: API, changeOrigin: false } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test-setup.ts"],
  },
});
