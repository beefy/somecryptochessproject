import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * The explorer is React Native for Web plus Material Design (react-native-paper),
 * so `react-native` resolves to `react-native-web` and the dev server proxies
 * `/v1` to a local node. `base: "./"` keeps the bundle path-independent: the
 * node serves it from `/` and a static host can publish it from a subdirectory.
 */
export default defineConfig(({ mode }) => ({
  base: "./",
  plugins: [react()],
  resolve: {
    alias: {
      "react-native": "react-native-web",
    },
    extensions: [".web.tsx", ".web.ts", ".web.jsx", ".web.js", ".tsx", ".ts", ".jsx", ".js", ".json"],
  },
  define: {
    global: "window",
    __DEV__: JSON.stringify(mode !== "production"),
    "process.env.NODE_ENV": JSON.stringify(mode === "production" ? "production" : "development"),
  },
  optimizeDeps: {
    esbuildOptions: {
      // react-native-web ships untranspiled `import ... from './x'` JSX with
      // Node-style extensionless specifiers.
      loader: { ".js": "jsx" },
      resolveExtensions: [".web.js", ".web.jsx", ".js", ".jsx", ".json"],
      jsx: "automatic",
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/v1": process.env.SCCP_NODE ?? "http://127.0.0.1:8645",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
}));
