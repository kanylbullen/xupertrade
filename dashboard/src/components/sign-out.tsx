"use client";

import { useEffect, useState, useTransition } from "react";
import { useRouter } from "next/navigation";

/** Renders nothing when auth is disabled. Otherwise a small Sign Out link. */
export function SignOut() {
  const router = useRouter();
  const [show, setShow] = useState(false);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    fetch("/api/auth/config", { cache: "no-store" })
      .then((r) => r.json())
      .then((cfg: { mode: string }) => setShow(cfg.mode !== "disabled"))
      .catch(() => setShow(false));
  }, []);

  if (!show) return null;

  return (
    <button
      onClick={() =>
        startTransition(async () => {
          await fetch("/api/auth/logout", { method: "POST" }).catch(() => null);
          router.push("/login");
          router.refresh();
        })
      }
      disabled={isPending}
      className="text-xs text-muted-foreground hover:text-foreground transition-colors"
      title="Sign out"
    >
      {isPending ? "…" : "Sign out"}
    </button>
  );
}
