import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// One ES module that HA loads as a custom panel (see custom_components/plejd_cloud/panel.py).
export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": JSON.stringify("production") },
  build: {
    outDir: "../custom_components/plejd_cloud/www",
    emptyOutDir: true,
    lib: { entry: "src/main.tsx", formats: ["es"], fileName: () => "panel.js" },
  },
});
