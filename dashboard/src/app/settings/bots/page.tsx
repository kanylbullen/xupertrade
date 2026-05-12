import { BotsClient } from "./bots-client";

export const dynamic = "force-dynamic";

export default function BotsPage() {
  return (
    <main className="container mx-auto max-w-2xl p-6 space-y-6">
      <header>
        <h1 className="text-2xl font-bold tracking-tight">Bots</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Per-tenant trading bot containers. One bot per mode
          (paper, testnet, mainnet). Requires credentials to be set
          and unlocked first.
        </p>
      </header>
      <BotsClient />
    </main>
  );
}
