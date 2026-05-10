import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "node",
    // Default: unit tests only. Integration tests (`*.integration.test.ts`)
    // need Docker for testcontainers — opt in via `npm run test:integration`.
    include: ["src/**/__tests__/**/*.test.ts"],
    exclude: ["**/*.integration.test.ts", "node_modules/**"],
  },
});
