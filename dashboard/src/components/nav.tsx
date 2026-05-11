"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { ModeSwitch } from "@/components/mode-switch";
import { BotStatusIndicator } from "@/components/bot-status-indicator";
import { UserMenu } from "@/components/user-menu";

// Options moved into the UserMenu (renamed Settings). Sign out also
// lives there now — keeps the top bar focused on navigation, with
// account-level actions tucked into the avatar dropdown.
const links = [
  { href: "/", label: "Overview" },
  { href: "/trades", label: "Trades" },
  { href: "/strategies", label: "Strategies" },
  { href: "/hodl", label: "HODL" },
  { href: "/vaults", label: "Vaults" },
  { href: "/status", label: "Status" },
];

export function Nav() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const modeParam = searchParams.get("mode");
  const suffix = modeParam ? `?mode=${modeParam}` : "";

  // Public pages (login) shouldn't show the in-app nav — those links
  // would 307 back to /login anyway. Cleaner to render nothing.
  if (pathname === "/login") return null;

  return (
    <nav className="border-b bg-background">
      <div className="mx-auto max-w-6xl px-4">
        {/* Row 1: brand + mode switch */}
        <div className="flex h-12 items-center justify-between sm:h-14">
          <Link
            href={`/${suffix}`}
            className="flex items-center gap-2 text-lg font-bold tracking-tight"
          >
            HyperTrade
            <BotStatusIndicator />
          </Link>
          <div className="flex items-center gap-3">
            {/* links inline on sm+, hidden here */}
            <div className="hidden sm:flex gap-4">
              {links.map((link) => (
                <Link
                  key={link.href}
                  href={`${link.href}${suffix}`}
                  className="text-sm text-muted-foreground transition-colors hover:text-foreground"
                >
                  {link.label}
                </Link>
              ))}
            </div>
            <ModeSwitch />
            <UserMenu suffix={suffix} />
          </div>
        </div>
        {/* Row 2: links on mobile only */}
        <div className="flex sm:hidden gap-5 overflow-x-auto pb-2 text-sm">
          {links.map((link) => (
            <Link
              key={link.href}
              href={`${link.href}${suffix}`}
              className="shrink-0 text-muted-foreground transition-colors hover:text-foreground"
            >
              {link.label}
            </Link>
          ))}
        </div>
      </div>
    </nav>
  );
}
