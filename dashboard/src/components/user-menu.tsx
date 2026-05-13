"use client";

import { useEffect, useState, useTransition } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Menu } from "@base-ui/react/menu";

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
 * Sidebar-footer user menu. Shows the signed-in identity + a dropdown
 * with Credentials / Bots / Settings / Sign out.
 *
 * Implementation note (Operator feedback fix): previously this used a
 * `useState` + absolutely-positioned `<div>`, which got clipped by the
 * `SidebarFooter`'s overflow and stacked under `<SidebarInset>` when
 * placed in the sidebar. We now use the base-ui `Menu` primitive which
 * renders the popup in a `Portal` and floats it with collision-aware
 * positioning — works in both expanded and collapsed (icon-only)
 * sidebar mode without any z-index gymnastics.
 *
 * Renders nothing in disabled-auth mode (no identity to show, no
 * sign-out to do).
 */
export function UserMenu() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [authMode, setAuthMode] = useState<string>(DEFAULT_AUTH_MODE);
  const [isPending, startTransition] = useTransition();

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

  if (authMode === "disabled") return null;
  if (!me) {
    // Render placeholder so layout doesn't shift when /me resolves.
    return <div className="h-8 w-8 rounded-full bg-muted/30" />;
  }

  const label = me.displayName || me.email;
  const initial = (label[0] || "?").toUpperCase();

  return (
    <Menu.Root>
      <Menu.Trigger
        className="flex h-8 w-8 items-center justify-center rounded-full bg-muted text-sm font-semibold text-foreground hover:bg-muted/70 transition-colors outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring"
        title={label}
      >
        {initial}
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner sideOffset={8} side="top" align="start">
          <Menu.Popup className="z-50 w-64 rounded-lg border bg-background shadow-lg outline-none">
            <div className="border-b px-4 py-3">
              <p className="text-xs text-muted-foreground">Signed in as</p>
              <p className="text-sm font-medium truncate">{me.email}</p>
              {me.isOperator && (
                <p className="text-[10px] uppercase tracking-wide text-amber-400 mt-1">
                  Operator
                </p>
              )}
            </div>
            <Menu.Item
              className="block px-4 py-2 text-sm hover:bg-muted data-[highlighted]:bg-muted transition-colors outline-none cursor-pointer"
              render={<Link href="/settings/credentials">Credentials</Link>}
            />
            <Menu.Item
              className="block px-4 py-2 text-sm hover:bg-muted data-[highlighted]:bg-muted transition-colors outline-none cursor-pointer"
              render={<Link href="/settings/bots">Bots</Link>}
            />
            {/*
             * Decision 4 (operator-confirmed): Settings stays tenant-
             * accessible. Do NOT operator-gate this entry.
             */}
            <Menu.Item
              className="block px-4 py-2 text-sm hover:bg-muted data-[highlighted]:bg-muted transition-colors outline-none cursor-pointer"
              render={<Link href="/options">Settings</Link>}
            />
            <Menu.Item
              disabled={isPending}
              onClick={() =>
                startTransition(async () => {
                  await fetch("/api/auth/logout", { method: "POST" }).catch(
                    () => null
                  );
                  router.push("/login");
                  router.refresh();
                })
              }
              className="block w-full text-left px-4 py-2 text-sm hover:bg-muted data-[highlighted]:bg-muted transition-colors outline-none cursor-pointer disabled:opacity-50"
            >
              {isPending ? "Signing out…" : "Sign out"}
            </Menu.Item>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
