/**
 * Integration test config — opt-in via `npm run test:integration`.
 *
 * Spins up real Postgres containers per testcontainers-node; not
 * runnable on machines without Docker. Default `npm test` excludes
 * these so the regular dev cycle stays fast and Docker-free.
 */

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
    include: ["src/**/__tests__/**/*.integration.test.ts"],
    // Generous timeout — Postgres container start takes ~5s + we
    // run multiple operations per test.
    testTimeout: 60_000,
    hookTimeout: 60_000,
    // Run integration tests sequentially. Each one stops/starts a
    // Postgres container; running in parallel risks Docker resource
    // contention on the dev machine.
    fileParallelism: false,
  },
});
