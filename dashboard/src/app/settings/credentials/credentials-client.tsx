"use client";

import { useEffect, useRef, useState, useTransition } from "react";

import { TelegramLinkCard } from "@/components/telegram-link-card";
import { UnlockModal } from "@/components/unlock-modal";

import { formatExpiryBadge } from "./expiry";

type Me = {
  passphraseSet: boolean;
  unlocked: boolean;
};

type SecretRow = {
  key: string;
  updatedAt: string;
  expiresAt: string | null;
};

const EXPIRY_TRACKED_KEYS = new Set([
  "HYPERLIQUID_PRIVATE_KEY",
  "HYPERLIQUID_MAINNET_PRIVATE_KEY",
]);

function defaultExpiryDate(): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() + 180);
  return d.toISOString().slice(0, 10);
}

/**
 * The set of credential slots this UI exposes. Keys must match the
 * `[A-Z0-9_]{1,64}` pattern enforced by the secrets API. Bot mode
 * each slot is required for is informational — PR 1 stores them
 * blindly; PR 2 enforces presence at bot-start time.
 */
const SLOTS: ReadonlyArray<{
  key: string;
  label: string;
  hint: string;
}> = [
  {
    key: "HYPERLIQUID_MAINNET_PRIVATE_KEY",
    label: "HyperLiquid private key (mainnet)",
    hint: "Optional. Required only if you trade on mainnet.",
  },
  {
    key: "HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS",
    label: "HyperLiquid account address (mainnet)",
    hint: "Optional. Required only if you trade on mainnet.",
  },
  {
    key: "HYPERLIQUID_PRIVATE_KEY",
    label: "HyperLiquid private key (testnet)",
    hint: "0x + 64 hex characters. From the HL testnet API wallet.",
  },
  {
    key: "HYPERLIQUID_ACCOUNT_ADDRESS",
    label: "HyperLiquid account address (testnet)",
    hint: "0x + 40 hex characters. The wallet that owns the API key.",
  },
  {
    key: "TELEGRAM_BOT_TOKEN",
    label: "Telegram bot token",
    hint: "Optional. Get one from @BotFather. Format: digits:base64.",
  },
  {
    key: "TELEGRAM_CHAT_ID",
    label: "Telegram chat ID",
    hint: "Optional. Numeric ID of the chat where the bot posts updates.",
  },
  {
    key: "VAULT_TRACKING_ADDRESS",
    label: "Vault tracking address (optional)",
    hint: "0x + 40 hex. Defaults to your mainnet account address. Override only if you want to monitor a different wallet's vault holdings.",
  },
];

export function CredentialsClient() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const r = await fetch("/api/tenant/me", { cache: "no-store" });
      if (!r.ok) {
        setError(`failed to load tenant state (${r.status})`);
        return;
      }
      setMe((await r.json()) as Me);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  if (error) {
    return (
      <div className="rounded border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-500">
        {error}
      </div>
    );
  }
  if (me === null) {
    return <div className="text-sm text-muted-foreground">Loading…</div>;
  }

  if (!me.passphraseSet) {
    return <SetPassphrase onDone={refresh} />;
  }
  if (!me.unlocked) {
    return (
      <>
        <p className="text-sm text-muted-foreground">
          Locked. Enter your passphrase to view or change credentials.
        </p>
        <UnlockModal onUnlocked={refresh} />
      </>
    );
  }
  return <CredentialsList onLocked={refresh} />;
}

function SetPassphrase({ onDone }: { onDone: () => void }) {
  const ref1 = useRef<HTMLInputElement>(null);
  const ref2 = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const a = ref1.current?.value ?? "";
    const b = ref2.current?.value ?? "";
    if (a.length < 12) {
      setError("Passphrase must be at least 12 characters");
      return;
    }
    if (a !== b) {
      setError("Passphrases don't match");
      return;
    }
    setError(null);
    startTransition(async () => {
      try {
        // 1. Set the passphrase (creates salt + verifier on tenant row).
        const setRes = await fetch("/api/tenant/me/passphrase", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ passphrase: a }),
        });
        if (!setRes.ok) {
          const data = await setRes.json().catch(() => null);
          setError(
            (data as { error?: string })?.error ??
              `Failed (${setRes.status})`,
          );
          return;
        }
        // 2. Unlock immediately so the user lands on the credentials
        // form without an extra prompt — they just typed the passphrase
        // twice, asking again would be hostile.
        const unlockRes = await fetch("/api/tenant/me/unlock", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ passphrase: a }),
        });
        if (!unlockRes.ok) {
          const data = await unlockRes.json().catch(() => null);
          setError(
            (data as { error?: string })?.error ??
              `Set succeeded but unlock failed (${unlockRes.status})`,
          );
          return;
        }
        // Clear DOM values before re-render dismounts the form.
        if (ref1.current) ref1.current.value = "";
        if (ref2.current) ref2.current.value = "";
        onDone();
      } catch (err) {
        setError(
          err instanceof Error
            ? `Network error: ${err.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  return (
    <form onSubmit={submit} className="space-y-4 rounded-lg border p-6">
      <div>
        <h2 className="text-lg font-semibold">Set your passphrase</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          This passphrase encrypts your stored credentials. We can&rsquo;t
          recover it — write it down. Minimum 12 characters.
        </p>
      </div>
      <div>
        <label className="text-sm font-medium" htmlFor="pp1">
          Passphrase
        </label>
        <input
          ref={ref1}
          id="pp1"
          type="password"
          name="new-passphrase"
          autoComplete="new-password"
          className="mt-1 w-full rounded border bg-background px-3 py-2 text-sm"
          disabled={isPending}
        />
      </div>
      <div>
        <label className="text-sm font-medium" htmlFor="pp2">
          Repeat passphrase
        </label>
        <input
          ref={ref2}
          id="pp2"
          type="password"
          name="new-passphrase-confirm"
          autoComplete="new-password"
          className="mt-1 w-full rounded border bg-background px-3 py-2 text-sm"
          disabled={isPending}
        />
      </div>
      {error && (
        <p className="text-sm text-red-500" role="alert">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={isPending}
        className="rounded bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
      >
        {isPending ? "Setting…" : "Set passphrase"}
      </button>
    </form>
  );
}

function CredentialsList({ onLocked }: { onLocked: () => void }) {
  const [secrets, setSecrets] = useState<SecretRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      const r = await fetch("/api/tenant/me/secrets", { cache: "no-store" });
      if (!r.ok) {
        setError(`failed to load secrets (${r.status})`);
        return;
      }
      const data = (await r.json()) as { secrets: SecretRow[] };
      setSecrets(data.secrets);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  useEffect(() => {
    refresh();
  }, []);

  if (error) {
    return (
      <div className="rounded border border-red-500/40 bg-red-500/10 p-4 text-sm text-red-500">
        {error}
      </div>
    );
  }
  if (secrets === null) {
    return <div className="text-sm text-muted-foreground">Loading…</div>;
  }

  const setKeys = new Set(secrets.map((s) => s.key));
  const updatedAtMap = new Map(secrets.map((s) => [s.key, s.updatedAt]));
  const expiresAtMap = new Map(secrets.map((s) => [s.key, s.expiresAt]));

  return (
    <div className="space-y-4">
      <SecurityInfo />
      <TelegramLinkCard />
      {SLOTS.map((slot) => (
        <SecretSlot
          key={slot.key}
          slot={slot}
          isSet={setKeys.has(slot.key)}
          updatedAt={updatedAtMap.get(slot.key)}
          expiresAt={expiresAtMap.get(slot.key) ?? null}
          onChange={refresh}
          onLocked={onLocked}
        />
      ))}
    </div>
  );
}

function SecretSlot({
  slot,
  isSet,
  updatedAt,
  expiresAt,
  onChange,
  onLocked,
}: {
  slot: { key: string; label: string; hint: string };
  isSet: boolean;
  updatedAt: string | undefined;
  expiresAt: string | null;
  onChange: () => void;
  /** Called when a write returns 401 — tenant got locked between the
   *  page load and this action (K-cache expired, user locked in
   *  another tab, etc.). Bubble up so the parent re-renders the
   *  unlock prompt instead of leaving the user stuck on an error. */
  onLocked: () => void;
}) {
  const tracksExpiry = EXPIRY_TRACKED_KEYS.has(slot.key);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const ref = useRef<HTMLInputElement>(null);
  const [expiryInput, setExpiryInput] = useState<string>("");

  // When the user opens the edit form for an HL key, seed the date
  // picker with the existing expires_at (if any) or today+180d as a
  // sensible default. They can clear it to drop expiry tracking.
  function startEditing() {
    if (tracksExpiry) {
      setExpiryInput(
        expiresAt ? expiresAt.slice(0, 10) : defaultExpiryDate(),
      );
    }
    setEditing(true);
  }

  function save(e: React.FormEvent) {
    e.preventDefault();
    const value = ref.current?.value ?? "";
    if (!value) {
      setError("Value required");
      return;
    }
    setError(null);
    startTransition(async () => {
      try {
        const body: { value: string; expiresAt?: string | null } = { value };
        if (tracksExpiry) {
          body.expiresAt = expiryInput || null;
        }
        const res = await fetch(
          `/api/tenant/me/secrets/${encodeURIComponent(slot.key)}`,
          {
            method: "PUT",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
          },
        );
        if (res.status === 401) {
          // K-cache expired since page load. Pop the unlock prompt
          // instead of dead-ending the user. Their pasted value is
          // lost; that's acceptable — they're about to type a
          // passphrase and we don't want to keep secret material
          // around longer than necessary.
          if (ref.current) ref.current.value = "";
          setEditing(false);
          onLocked();
          return;
        }
        if (!res.ok) {
          const data = await res.json().catch(() => null);
          setError(
            (data as { error?: string })?.error ?? `Failed (${res.status})`,
          );
          return;
        }
        if (ref.current) ref.current.value = "";
        setEditing(false);
        onChange();
      } catch (err) {
        setError(
          err instanceof Error
            ? `Network error: ${err.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  function remove() {
    if (!confirm(`Delete ${slot.label}? This cannot be undone.`)) return;
    setError(null);
    startTransition(async () => {
      try {
        const res = await fetch(
          `/api/tenant/me/secrets/${encodeURIComponent(slot.key)}`,
          { method: "DELETE" },
        );
        if (!res.ok && res.status !== 404) {
          const data = await res.json().catch(() => null);
          setError(
            (data as { error?: string })?.error ?? `Failed (${res.status})`,
          );
          return;
        }
        onChange();
      } catch (err) {
        setError(
          err instanceof Error
            ? `Network error: ${err.message}`
            : "Network error — check your connection and try again.",
        );
      }
    });
  }

  return (
    <div className="rounded-lg border p-4">
      <div className="flex items-center justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium truncate">{slot.label}</h3>
            <span
              className={
                isSet
                  ? "rounded-full bg-green-500/20 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-green-500"
                  : "rounded-full bg-muted px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground"
              }
            >
              {isSet ? "Set" : "Not set"}
            </span>
          </div>
          <p className="mt-0.5 text-xs text-muted-foreground">{slot.hint}</p>
          {isSet && updatedAt && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              Updated {new Date(updatedAt).toLocaleString()}
            </p>
          )}
          {tracksExpiry && expiresAt && (() => {
            const b = formatExpiryBadge(expiresAt);
            const cls =
              b.tone === "bad"
                ? "bg-red-500/20 text-red-500"
                : b.tone === "warn"
                  ? "bg-amber-500/20 text-amber-500"
                  : "bg-muted text-muted-foreground";
            return (
              <span
                className={`mt-1 inline-block rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide ${cls}`}
              >
                {b.text}
              </span>
            );
          })()}
        </div>
        {!editing && (
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              onClick={startEditing}
              disabled={isPending}
              className="rounded border px-3 py-1 text-xs hover:bg-muted disabled:opacity-50"
            >
              {isSet ? "Replace" : "Set"}
            </button>
            {isSet && (
              <button
                type="button"
                onClick={remove}
                disabled={isPending}
                className="rounded border border-red-500/40 px-3 py-1 text-xs text-red-500 hover:bg-red-500/10 disabled:opacity-50"
              >
                Delete
              </button>
            )}
          </div>
        )}
      </div>
      {editing && (
        <form onSubmit={save} className="mt-3 space-y-2">
          <input
            ref={ref}
            type="password"
            name={`secret-${slot.key.toLowerCase()}`}
            autoComplete="off"
            className="w-full rounded border bg-background px-3 py-2 text-sm"
            placeholder="Paste value here"
            disabled={isPending}
          />
          {tracksExpiry && (
            <div className="flex items-center gap-2">
              <label
                className="text-xs text-muted-foreground"
                htmlFor={`exp-${slot.key}`}
              >
                Expires:
              </label>
              <input
                id={`exp-${slot.key}`}
                type="date"
                value={expiryInput}
                onChange={(e) => setExpiryInput(e.target.value)}
                disabled={isPending}
                className="rounded border bg-background px-2 py-1 text-xs"
              />
              <span className="text-[11px] text-muted-foreground">
                Empty = no reminders. Default = today + 180 days.
              </span>
            </div>
          )}
          {error && (
            <p className="text-sm text-red-500" role="alert">
              {error}
            </p>
          )}
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={isPending}
              className="rounded bg-primary px-3 py-1 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
            >
              {isPending ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setError(null);
                if (ref.current) ref.current.value = "";
              }}
              disabled={isPending}
              className="rounded border px-3 py-1 text-xs hover:bg-muted disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

function SecurityInfo() {
  return (
    <div className="rounded-lg border border-blue-500/40 bg-blue-500/5 p-4 text-sm">
      <div className="flex items-start gap-3">
        <span aria-hidden className="text-base">🔒</span>
        <div className="space-y-2">
          <p className="font-medium">
            Your private keys are encrypted under a key only your
            passphrase can unlock &mdash; never written to disk, never
            visible to the database or to logs.
          </p>
          <p className="text-muted-foreground">
            The keys travel from your browser to the dashboard server
            over TLS. The server encrypts them in memory using a key
            derived from the passphrase you entered when you unlocked
            this page, and only the encrypted blob hits the database.
            Plaintext never touches durable storage &mdash; not the
            DB, not the log files, not Phase. If you forget the
            passphrase the keys are unrecoverable; there is no reset.
            If someone steals the database, they still need to crack
            your passphrase through Argon2id before they get anything
            usable, which is intentionally tuned to take years on
            dedicated hardware for any decent passphrase.
          </p>
          <details className="group mt-2">
            <summary className="cursor-pointer text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
              For the technically curious &mdash; how it actually works
            </summary>
            <div className="mt-3 space-y-3 text-xs text-muted-foreground">
              <p>
                <strong className="text-foreground">Key derivation.</strong>{" "}
                Your passphrase is fed through{" "}
                <code className="rounded bg-muted px-1">Argon2id</code>{" "}
                (memory-hard KDF, parameters: 64&nbsp;MiB, 3 iterations,
                4-way parallelism) together with a 16-byte random salt
                that&rsquo;s unique per tenant. Output is a 32-byte
                key&nbsp;<code className="rounded bg-muted px-1">K</code>.
                These parameters mean an attacker with a stolen
                database needs ~hundreds of milliseconds per guess on a
                modern CPU and can&rsquo;t shortcut it on a GPU/ASIC the
                way they could with bcrypt or PBKDF2.
              </p>
              <p>
                <strong className="text-foreground">At-rest encryption.</strong>{" "}
                Each secret value is encrypted with{" "}
                <code className="rounded bg-muted px-1">AES-256-GCM</code>{" "}
                under&nbsp;<code className="rounded bg-muted px-1">K</code>,
                using a fresh 12-byte nonce per write. The database
                stores only the ciphertext + nonce; the key is never
                persisted to disk. Two encryptions of the same
                plaintext produce different ciphertexts (so a DB-read
                alone doesn&rsquo;t even reveal which secrets share a
                value). GCM authenticates the ciphertext &mdash; any
                tampering or wrong-key decryption attempt fails loudly
                rather than returning garbage.
              </p>
              <p>
                <strong className="text-foreground">Verifier, not password storage.</strong>{" "}
                We never store your passphrase, not even hashed. What
                we store is an{" "}
                <code className="rounded bg-muted px-1">HMAC-SHA-256</code>{" "}
                of&nbsp;<code className="rounded bg-muted px-1">K</code>{" "}
                over a fixed domain string. On unlock we re-derive K
                from your typed passphrase and compare in constant
                time. Wrong passphrase → verifier mismatch → we
                don&rsquo;t even attempt decryption.
              </p>
              <p>
                <strong className="text-foreground">Session cache.</strong>{" "}
                After unlock,&nbsp;<code className="rounded bg-muted px-1">K</code>{" "}
                lives in Redis under a key tied to your session
                ID&nbsp;with a 24-hour TTL, so you don&rsquo;t have to
                re-enter the passphrase on every action. Logging out or
                hitting Lock clears it immediately. Key length is
                checked on read &mdash; corrupted entries are dropped
                and you get re-prompted.
              </p>
              <p>
                <strong className="text-foreground">Threat boundaries
                we don&rsquo;t cover.</strong> An operator with root
                access to the host running the bot containers could
                read decrypted secrets out of running-process memory or
                container env vars (the bot needs the plaintext key to
                place orders &mdash; that&rsquo;s unavoidable). The
                relevant property is that no decrypted secret ever
                touches durable storage, no operator can read your
                secrets without your passphrase before bot start, and
                rotating a compromised key invalidates the old
                ciphertext on next save.
              </p>
              <p className="text-[11px]">
                Source:{" "}
                <code className="rounded bg-muted px-1">
                  dashboard/src/lib/crypto/&#123;passphrase,secrets,k-cache&#125;.ts
                </code>
              </p>
            </div>
          </details>
        </div>
      </div>
    </div>
  );
}
