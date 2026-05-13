import { verifyUnlockToken } from "@/lib/unlock-token";

import { UnlockClient } from "./unlock-client";

export const dynamic = "force-dynamic";

type SearchParams = Promise<{ token?: string }>;

/**
 * /unlock?token=...   (PR 3c)
 *
 * Landing page for the Telegram unlock-deeplink flow. Validates
 * the signed token server-side (cheap; just HMAC + JSON.parse)
 * and renders a client component that prompts for the passphrase
 * and calls the existing /api/tenant/me/unlock endpoint.
 *
 * SECURITY (H-4): this route is in PUBLIC_PATHS and may be loaded
 * unauthenticated. We deliberately render NO tenant-identifying
 * info — no email, no display name, no tenant id — so a forwarded
 * or screenshot-shared link doesn't disclose who the link belongs
 * to. The actual passphrase POST still requires a valid session
 * (`requireTenant` in /api/tenant/me/unlock), so we don't need the
 * tenant id in the form either; the session decides which tenant
 * is being unlocked.
 *
 * Token-validation errors collapse into a single generic message —
 * we never distinguish "valid signature, tenant doesn't exist" from
 * "expired" or "tampered with", since that would let an attacker
 * with a valid-looking token probe whether a given tenant id exists.
 *
 * If the token is missing/invalid/expired we render an error
 * card — never redirect to /login, because the deeplink-clicker
 * may not yet have an authenticated session (this is the whole
 * point of the flow).
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

  // Pure crypto check — does NOT touch the DB. Either the token's
  // signature + expiry are valid (continue) or they aren't (generic
  // error; intentionally indistinguishable from "tenant unknown").
  const payload = await verifyUnlockToken(token);
  if (payload === null) {
    return (
      <ErrorCard
        title="Link expired or invalid"
        body="This unlock link has expired or is no longer valid. Open the dashboard or your bot's Telegram chat to generate a fresh one."
      />
    );
  }

  return (
    <main className="container mx-auto max-w-sm p-6 space-y-6 mt-12">
      <header>
        <h1 className="text-2xl font-bold tracking-tight">Unlock your bot</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Enter your passphrase to resume your bot.
        </p>
      </header>
      <UnlockClient />
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
