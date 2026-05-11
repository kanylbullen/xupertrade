"use client";

import { useEffect, useRef, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

type Me = {
  id: string;
  email: string;
  displayName: string | null;
  isOperator: boolean;
};

type AuthCfg = { mode: string };

// Default to "disabled" so we don't flash a placeholder before
// /api/auth/config resolves on disabled-auth deploys (matches the
// old SignOut component's render-nothing behavior). The fetch
// flips it to the real mode once it lands.
const DEFAULT_AUTH_MODE = "disabled";

/**
 * Top-right user menu. Shows the signed-in identity + a dropdown with
 * Settings (was "Options" in the main nav) and Sign out.
 *
 * Renders nothing in disabled-auth mode (no identity to show, no
 * sign-out to do).
 */
export function UserMenu({ suffix }: { suffix: string }) {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [authMode, setAuthMode] = useState<string>(DEFAULT_AUTH_MODE);
  const [open, setOpen] = useState(false);
  const [isPending, startTransition] = useTransition();
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/api/auth/config", { cache: "no-store" })
      .then((r) => {
        // Bot-unreachable returns 503 with {error:"bot-unreachable"};
        // don't try to parse as AuthCfg — fail safe to disabled mode
        // so the menu doesn't get stuck in a placeholder.
        if (!r.ok) return null;
        return r.json() as Promise<AuthCfg>;
      })
      .then((cfg) => {
        if (cfg && typeof cfg.mode === "string") setAuthMode(cfg.mode);
      })
      .catch(() => undefined);
    fetch("/api/tenant/me", { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<Me>) : null))
      .then((data) => setMe(data))
      .catch(() => setMe(null));
  }, []);

  // Close on outside click + Escape
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    if (open) {
      document.addEventListener("mousedown", onClick);
      document.addEventListener("keydown", onKey);
    }
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (authMode === "disabled") return null;
  if (!me) {
    // Render placeholder so layout doesn't shift when /me resolves.
    return <div className="h-8 w-8 rounded-full bg-muted/30" />;
  }

  const label = me.displayName || me.email;
  const initial = (label[0] || "?").toUpperCase();

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex h-8 w-8 items-center justify-center rounded-full bg-muted text-sm font-semibold text-foreground hover:bg-muted/70 transition-colors"
        aria-haspopup="menu"
        aria-expanded={open}
        title={label}
      >
        {initial}
      </button>

      {open && (
        // Plain popover semantics — no role="menu" because we don't
        // implement the full menu keyboard contract (arrow nav, focus
        // trap). Escape-to-close is handled via the document listener
        // above. The contained <Link> + <button> are individually
        // focusable/Tab-navigable which is sufficient for a 2-item
        // dropdown.
        <div className="absolute right-0 mt-2 w-64 rounded-lg border bg-background shadow-lg z-50">
          <div className="border-b px-4 py-3">
            <p className="text-xs text-muted-foreground">Signed in as</p>
            <p className="text-sm font-medium truncate">{me.email}</p>
            {me.isOperator && (
              <p className="text-[10px] uppercase tracking-wide text-amber-400 mt-1">
                Operator
              </p>
            )}
          </div>
          <Link
            href={`/options${suffix}`}
            className="block px-4 py-2 text-sm hover:bg-muted transition-colors"
            onClick={() => setOpen(false)}
          >
            Settings
          </Link>
          <button
            onClick={() =>
              startTransition(async () => {
                await fetch("/api/auth/logout", { method: "POST" }).catch(() => null);
                setOpen(false);
                router.push("/login");
                router.refresh();
              })
            }
            disabled={isPending}
            className="block w-full text-left px-4 py-2 text-sm hover:bg-muted transition-colors disabled:opacity-50"
          >
            {isPending ? "Signing out…" : "Sign out"}
          </button>
        </div>
      )}
    </div>
  );
}
