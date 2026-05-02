"use client";

import { useSearchParams } from "next/navigation";

export type Mode = "paper" | "testnet" | "mainnet";

export function useMode(): Mode {
  const sp = useSearchParams();
  const m = sp.get("mode");
  return m === "paper" || m === "mainnet" ? m : "testnet";
}

export function withMode(path: string, mode: Mode): string {
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}mode=${mode}`;
}
