import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      // `server-only` exports a single throw-on-import line that
      // fires when imported into a Client Component bundle. Under
      // vitest there's no client/server distinction, so the import
      // throws unconditionally. Alias to an empty module — the
      // import statement becomes a no-op for test purposes; the
      // build-time guarantee still applies in the actual Next
      // production build.
      "server-only": path.resolve(__dirname, "./src/test/server-only-stub.ts"),
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
