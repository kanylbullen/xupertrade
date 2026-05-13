"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
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

/**
 * Dashboard sidebar — sole nav surface after the cutover (PR B + C of
 * the nav refactor merged into one). The top-bar `<Nav />` and
 * `<ModeSwitch />` are gone; mode is route-bound on the Overview only,
 * mode-agnostic everywhere else.
 *
 * Layout:
 * - Header: brand + BotStatusIndicator (reads `/overview/<mode>` from
 *   the pathname; defaults to testnet on mode-agnostic routes).
 * - Overview group: 3 mode-bound links → /overview/{paper,testnet,mainnet}.
 * - Pages group: bare paths (no `?mode=`) — Trades is mode-agnostic
 *   with its own filter pill, Strategies is hardcoded descriptive
 *   cards, HODL + Vaults are mainnet-only by design.
 * - Footer: `<UserMenu />` — Credentials / Bots / Settings / Sign out.
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

  // Mirrors the deleted `nav.tsx:29` — public pages (login) hide nav chrome.
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
      <SidebarFooter>
        <UserMenu />
      </SidebarFooter>
    </Sidebar>
  );
}
