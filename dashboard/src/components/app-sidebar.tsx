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
 * - Pages group: mode-agnostic links (Trades, Strategies, HODL, Vaults).
 *   HODL + Vaults link to bare `/hodl` and `/vaults` per Decision 2
 *   (mainnet-only by design; no `?mode=` suffix).
 */

const overviewModes = [
  { href: "/overview/paper", label: "Paper" },
  { href: "/overview/testnet", label: "Testnet" },
  { href: "/overview/mainnet", label: "Mainnet" },
] as const;

const pageLinks = [
  { href: "/trades", label: "Trades" },
  { href: "/strategies", label: "Strategies" },
  { href: "/hodl", label: "HODL" },
  { href: "/vaults", label: "Vaults" },
] as const;

export function AppSidebar() {
  const pathname = usePathname();

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
      </SidebarContent>
    </Sidebar>
  );
}
