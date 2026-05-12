import { redirect } from "next/navigation";

import { db, tenants } from "@/lib/db";
import { eq } from "drizzle-orm";
import { verifyUnlockToken } from "@/lib/unlock-token";

import { UnlockClient } from "./unlock-client";

export const dynamic = "force-dynamic";

type SearchParams = Promise<{ token?: string }>;

/**
 * /unlock?token=...   (PR 3c)
 *
 * Landing page for the Telegram unlock-deeplink flow. Validates
 * the signed token server-side (cheap; just HMAC + JSON.parse),
 * loads the tenant's display name for confirmation, and renders
 * a client component that prompts for the passphrase and calls
 * the existing /api/tenant/me/unlock endpoint.
 *
 * If the token is missing/invalid/expired we render an error
 * card — never redirect to /login, because the deeplink-clicker
 * may not yet have an authenticated session (this is the whole
 * point of the flow). The unlock POST below will require a
 * session, so the page also surfaces a "sign in first" hint
 * when applicable.
 */
export default async function UnlockPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const params = await searchParams;
  const token = params.token ?? "";

  if (!token) {
    return (
      <ErrorCard
        title="Missing unlock link"
        body="The unlock link is missing its token. Generate a fresh one from your bot's Telegram chat or your dashboard."
      />
    );
  }

  const payload = await verifyUnlockToken(token);
  if (payload === null) {
    return (
      <ErrorCard
        title="Link expired or invalid"
        body="The unlock link is no longer valid (expired or tampered with). Trigger a fresh one from the dashboard."
      />
    );
  }

  // Look up the tenant for display only — confirms the user sees
  // "Unlock for alice@example.com" before typing their passphrase
  // and notices if they accidentally clicked someone else's link.
  const rows = await db
    .select({
      id: tenants.id,
      email: tenants.email,
      displayName: tenants.displayName,
    })
    .from(tenants)
    .where(eq(tenants.id, payload.sub))
    .limit(1);
  const tenant = rows[0];
  if (!tenant) {
    return (
      <ErrorCard
        title="Tenant not found"
        body="The unlock link references a tenant that no longer exists. If this is your account, contact the operator."
      />
    );
  }

  return (
    <main className="container mx-auto max-w-sm p-6 space-y-6 mt-12">
      <header>
        <h1 className="text-2xl font-bold tracking-tight">Unlock bot</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Enter your passphrase to resume your bot.
        </p>
      </header>
      <UnlockClient
        tenantId={tenant.id}
        tenantLabel={tenant.displayName || tenant.email}
      />
    </main>
  );
}

function ErrorCard({ title, body }: { title: string; body: string }) {
  return (
    <main className="container mx-auto max-w-sm p-6 mt-12">
      <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-6">
        <h1 className="text-lg font-semibold text-red-500">{title}</h1>
        <p className="mt-2 text-sm text-muted-foreground">{body}</p>
      </div>
    </main>
  );
}
