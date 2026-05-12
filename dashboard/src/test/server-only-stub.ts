// vitest alias target for `server-only` — see vitest.config.ts.
// Empty by design: under tests there's no client/server bundle
// distinction so the real `server-only` module's throw doesn't
// apply. The production Next build still uses the real package.
export {};
