import { CredentialsClient } from "./credentials-client";

export const dynamic = "force-dynamic";

export default function CredentialsPage() {
  return (
    <main className="container mx-auto max-w-2xl p-6 space-y-6">
      <header>
        <h1 className="text-2xl font-bold tracking-tight">Credentials</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Per-tenant exchange keys + Telegram bot token. Stored encrypted
          with your passphrase — operator cannot read them.
        </p>
      </header>
      <CredentialsClient />
    </main>
  );
}
