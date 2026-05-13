"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { BotStatusIndicator } from "@/components/bot-status-indicator";
import { useMode } from "@/lib/use-mode";

/**
 * Dashboard sidebar — PR A of the nav refactor. Renders **alongside**
 * the existing top-bar `<Nav />` for one PR cycle; PR C removes Nav
 * and this becomes the sole navigation surface.
 *
 * Layout per the plan:
 * - Header: brand + BotStatusIndicator (mode-aware via useMode — for
 *   PR A this still tracks `?mode=` since `use-mode.ts` is untouched;
 *   PR B/C rewire it to read the route).
 * - Overview group: 3 mode-bound links → /overview/{paper,testnet,mainnet}.
 * - Pages group:
 *     - Trades, Strategies: propagate the active `?mode=` until PR B
 *       rewires them to be mode-agnostic. Without this, clicking the
 *       sidebar link from `/?mode=testnet` would silently reset to
 *       paper. Copilot review fix on PR #103.
 *     - HODL, Vaults: pinned to `?mode=mainnet` (Decision 2 — they
 *       only operate on the mainnet bot).
 */

const overviewModes = [
  { href: "/overview/paper", label: "Paper" },
  { href: "/overview/testnet", label: "Testnet" },
  { href: "/overview/mainnet", label: "Mainnet" },
] as const;

const transitionalPageLinks = [
  { base: "/trades", label: "Trades", pinMainnet: false },
  { base: "/strategies", label: "Strategies", pinMainnet: false },
  { base: "/hodl", label: "HODL", pinMainnet: true },
  { base: "/vaults", label: "Vaults", pinMainnet: true },
] as const;

export function AppSidebar() {
  const pathname = usePathname();
  const activeMode = useMode();

  // Mirrors `nav.tsx:29` — public pages (login) hide nav chrome.
  if (pathname === "/login") return null;

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader>
        <div className="flex items-center gap-2 px-2 py-1 text-lg font-bold tracking-tight">
          <span>Xupertrade</span>
          <BotStatusIndicator />
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
            {transitionalPageLinks.map((link) => {
              const mode = link.pinMainnet ? "mainnet" : activeMode;
              const href = `${link.base}?mode=${mode}`;
              return (
                <SidebarMenuItem key={link.base}>
                  <SidebarMenuButton
                    isActive={pathname === link.base}
                    tooltip={link.label}
                    render={
                      <Link href={href}>
                        <span>{link.label}</span>
                      </Link>
                    }
                  />
                </SidebarMenuItem>
              );
            })}
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
    </Sidebar>
  );
}
