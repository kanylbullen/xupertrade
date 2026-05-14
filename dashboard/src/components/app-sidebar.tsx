"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { BotStatusIndicator } from "@/components/bot-status-indicator";
import { UserMenu } from "@/components/user-menu";
import type { Mode } from "@/lib/mode";

/**
 * Dashboard sidebar — sole nav surface after the cutover (PR B + C of
 * the nav refactor merged into one). The top-bar `<Nav />` and
 * `<ModeSwitch />` are gone; mode is route-bound on the Overview only,
 * mode-agnostic everywhere else.
 *
 * Layout:
 * - Header: brand only. (The global `BotStatusIndicator` used to live
 *   here too — replaced by per-mode dots in the Overview group below
 *   so the operator can see all three bots' state at once.)
 * - Overview group: 3 mode-bound links → /overview/{mainnet,testnet,paper}.
 *   Real-money first, scratch last (operator preference). Each row has
 *   its own status dot (green/yellow/red/muted) polled independently.
 * - Pages group: bare paths (no `?mode=`) — Trades is mode-agnostic
 *   with its own filter pill, Strategies is hardcoded descriptive
 *   cards, HODL + Vaults are mainnet-only by design.
 * - Footer: `<UserMenu />` — Credentials / Bots / Settings / Sign out.
 */

const overviewModes: ReadonlyArray<{
  href: string;
  label: string;
  mode: Mode;
}> = [
  { href: "/overview/mainnet", label: "Mainnet", mode: "mainnet" },
  { href: "/overview/testnet", label: "Testnet", mode: "testnet" },
  { href: "/overview/paper", label: "Paper", mode: "paper" },
];

const pageLinks = [
  { href: "/trades", label: "Trades" },
  { href: "/strategies", label: "Strategies" },
  { href: "/hodl", label: "HODL" },
  { href: "/vaults", label: "Vaults" },
] as const;

export function AppSidebar() {
  const pathname = usePathname();
  const [isOperator, setIsOperator] = useState(false);

  useEffect(() => {
    fetch("/api/tenant/me", { cache: "no-store" })
      .then((r) => (r.ok ? (r.json() as Promise<{ isOperator?: boolean }>) : null))
      .then((j) => setIsOperator(j?.isOperator === true))
      .catch(() => undefined);
  }, []);

  // Mirrors the deleted `nav.tsx:29` — public pages (login) hide nav chrome.
  if (pathname === "/login") return null;

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-1 text-lg font-bold tracking-tight">
          <span>xupertrade</span>
        </div>
      </SidebarHeader>
      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel>Overview</SidebarGroupLabel>
          <SidebarMenu>
            {overviewModes.map((link) => (
              <SidebarMenuItem key={link.href}>
                <SidebarMenuButton
                  isActive={pathname === link.href}
                  tooltip={link.label}
                  render={
                    <Link href={link.href}>
                      <BotStatusIndicator
                        mode={link.mode}
                        variant="dot"
                      />
                      <span>{link.label}</span>
                    </Link>
                  }
                />
              </SidebarMenuItem>
            ))}
          </SidebarMenu>
        </SidebarGroup>
        <SidebarGroup>
          <SidebarGroupLabel>Pages</SidebarGroupLabel>
          <SidebarMenu>
            {pageLinks.map((link) => (
              <SidebarMenuItem key={link.href}>
                <SidebarMenuButton
                  isActive={pathname === link.href}
                  tooltip={link.label}
                  render={
                    <Link href={link.href}>
                      <span>{link.label}</span>
                    </Link>
                  }
                />
              </SidebarMenuItem>
            ))}
          </SidebarMenu>
        </SidebarGroup>
        {isOperator && (
          <SidebarGroup>
            <SidebarGroupLabel>Admin</SidebarGroupLabel>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={pathname === "/admin" || pathname.startsWith("/admin/")}
                  tooltip="Admin"
                  render={
                    <Link href="/admin">
                      <span>Tenants</span>
                    </Link>
                  }
                />
              </SidebarMenuItem>
              <SidebarMenuItem>
                <SidebarMenuButton
                  isActive={pathname === "/admin/server"}
                  tooltip="Server stats"
                  render={
                    <Link href="/admin/server">
                      <span>Server</span>
                    </Link>
                  }
                />
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroup>
        )}
      </SidebarContent>
      <SidebarFooter>
        <UserMenu />
      </SidebarFooter>
    </Sidebar>
  );
}
