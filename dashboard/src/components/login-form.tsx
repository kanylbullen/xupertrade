"use client";

import { useState, useTransition } from "react";
import { Button } from "@/components/ui/button";
// Shared with the server-side OIDC redirect path. This file used to
// carry a second copy of these rules, which meant the `/\evil.com`
// open redirect had to be fixed in two places — and `window.location
// .href` normalises the backslash exactly like `new URL` does, so the
// client copy was just as exploitable.
import { safeNext } from "@/lib/safe-next";

export function LoginForm({
  next,
  initialError,
}: {
  next: string;
  initialError: string;
}) {
  const [user, setUser] = useState("");
  const [pass, setPass] = useState("");
  const [error, setError] = useState(initialError);
  const [isPending, startTransition] = useTransition();
  const target = safeNext(next);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    startTransition(async () => {
      try {
        const res = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: user, password: pass }),
        });
        const data = (await res.json()) as { ok?: boolean; error?: string };
        if (!data.ok) {
          setError(data.error || "Login failed");
          return;
        }
        // Use full reload so the proxy sees the new cookie immediately and
        // any cached page state is discarded.
        window.location.href = target;
      } catch {
        setError("Network error");
      }
    });
  }

  return (
    <form
      onSubmit={submit}
      action="/api/auth/login"
      method="post"
      className="space-y-4 rounded-lg border bg-card p-6"
    >
      <div className="space-y-2">
        <label htmlFor="username" className="text-sm font-medium">
          Username
        </label>
        <input
          id="username"
          name="username"
          type="text"
          autoComplete="username"
          required
          value={user}
          onChange={(e) => setUser(e.target.value)}
          className="w-full rounded border bg-background px-3 py-2 text-sm"
          disabled={isPending}
        />
      </div>
      <div className="space-y-2">
        <label htmlFor="password" className="text-sm font-medium">
          Password
        </label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={pass}
          onChange={(e) => setPass(e.target.value)}
          className="w-full rounded border bg-background px-3 py-2 text-sm"
          disabled={isPending}
        />
      </div>
      {error && (
        <p className="text-sm text-red-400 border border-red-500/30 bg-red-500/5 rounded px-3 py-2">
          {error === "invalid-credentials" ? "Wrong username or password" : error}
        </p>
      )}
      <Button type="submit" disabled={isPending} className="w-full">
        {isPending ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
