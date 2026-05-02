"use client";

import { useRouter, usePathname, useSearchParams } from "next/navigation";

const MODES = [
  { value: "paper", label: "Paper", color: "bg-yellow-500/20 text-yellow-400" },
  { value: "testnet", label: "Testnet", color: "bg-blue-500/20 text-blue-400" },
  { value: "mainnet", label: "Mainnet", color: "bg-green-500/20 text-green-400" },
] as const;

export function ModeSwitch() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const mode = searchParams.get("mode") ?? "paper";

  function setMode(next: string) {
    router.push(`${pathname}?mode=${next}`);
  }

  return (
    <div className="flex items-center rounded-full border border-border bg-muted/50 p-0.5 text-xs">
      {MODES.map((m) => (
        <button
          key={m.value}
          onClick={() => setMode(m.value)}
          className={`rounded-full px-3 py-1 font-medium transition-all ${
            mode === m.value
              ? `${m.color} shadow-sm`
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}
