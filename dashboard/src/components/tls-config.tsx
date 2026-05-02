"use client";

import { useEffect, useRef, useState, useTransition } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";

type CaddyStatus = {
  reachable: boolean;
  status?: number;
  tls_subjects?: string[];
  issuer?: "acme" | "internal" | "unknown";
  servers?: string[];
  error?: string;
};

type TlsConfig = {
  enabled: boolean;
  domain: string;
  email: string;
  cf_token_set: boolean;
  caddy_status: CaddyStatus;
};

export function TlsConfig() {
  const [cfg, setCfg] = useState<TlsConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState("");
  const [isPending, startTransition] = useTransition();

  const domainRef = useRef<HTMLInputElement>(null);
  const emailRef = useRef<HTMLInputElement>(null);
  const tokenRef = useRef<HTMLInputElement>(null);
  // null until first load — prevents the toggle from briefly showing OFF
  // before refresh() syncs from server, which previously caused users to
  // accidentally save enabled=false right after the page loaded.
  const [enabled, setEnabled] = useState<boolean | null>(null);

  async function refresh() {
    try {
      const res = await fetch("/api/tls/config", { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as TlsConfig;
      setCfg(data);
      setEnabled(data.enabled);
      if (domainRef.current) domainRef.current.value = data.domain;
      if (emailRef.current) emailRef.current.value = data.email;
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  function save() {
    setSavedMsg("");
    setError(null);

    // Normalize domain: strip scheme, trailing slash, paths, ports.
    // Caddy expects a bare hostname like "hypertrade.xuper.fun".
    let domainVal = (domainRef.current?.value ?? "").trim();
    domainVal = domainVal.replace(/^https?:\/\//i, "");
    domainVal = domainVal.replace(/\/.*$/, "");  // strip path
    domainVal = domainVal.replace(/:\d+$/, "");   // strip port
    if (domainRef.current && domainRef.current.value !== domainVal) {
      domainRef.current.value = domainVal;  // reflect cleanup in UI
    }
    const emailVal = emailRef.current?.value.trim() ?? "";
    const tokenVal = tokenRef.current?.value ?? "";

    // Don't let a not-yet-loaded toggle cause a save
    if (enabled === null) {
      setError("Still loading — try again in a moment");
      return;
    }

    // Frontend validation when enabling — bot will reject anyway, but a
    // local error is faster and clearer.
    if (enabled) {
      const missing: string[] = [];
      if (!domainVal) missing.push("domain");
      if (!emailVal) missing.push("email");
      if (!tokenVal && !cfg?.cf_token_set) missing.push("Cloudflare API token");
      if (missing.length > 0) {
        setError(`Cannot enable HTTPS — missing: ${missing.join(", ")}`);
        return;
      }
    }

    startTransition(async () => {
      const body: Record<string, unknown> = {
        enabled,
        domain: domainVal,
        email: emailVal,
      };
      if (tokenVal) body.cf_token = tokenVal;
      try {
        const res = await fetch("/api/tls/configure", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const data = (await res.json()) as {
          ok?: boolean;
          error?: string;
          enabled?: boolean;
        };
        if (!data.ok) {
          setError(data.error || `HTTP ${res.status}`);
          return;
        }
        setSavedMsg(
          data.enabled
            ? "Saved — Caddy reloaded with HTTPS config. Cert issuance can take 30-60 seconds (DNS-01 challenge)."
            : "TLS disabled — Caddy now serving plain HTTP."
        );
        if (tokenRef.current) tokenRef.current.value = "";
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unknown error");
      }
    });
  }

  if (!cfg) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>HTTPS / TLS</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">Loading…</p>
        </CardContent>
      </Card>
    );
  }

  const s = cfg.caddy_status;
  const isAcme = s.issuer === "acme";
  const isInternal = s.issuer === "internal";
  const hasCert = s.reachable && (s.tls_subjects?.length ?? 0) > 0;
  const leActive = isAcme && hasCert;
  const lePending = cfg.enabled && !leActive;
  const selfSigned = !cfg.enabled && isInternal && hasCert;

  let badgeText = "UNKNOWN";
  let badgeColor = "border-muted-foreground text-muted-foreground";
  if (leActive) {
    badgeText = "LE ACTIVE";
    badgeColor = "border-green-500 text-green-400";
  } else if (lePending) {
    badgeText = "LE PENDING";
    badgeColor = "border-yellow-500 text-yellow-400";
  } else if (selfSigned) {
    badgeText = "SELF-SIGNED";
    badgeColor = "border-blue-500 text-blue-400";
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>HTTPS / TLS</CardTitle>
          <Badge variant="outline" className={badgeColor}>
            {badgeText}
          </Badge>
        </div>
        <p className="text-sm text-muted-foreground mt-2">
          The dashboard runs HTTPS by default with a self-signed cert from
          Caddy&apos;s internal CA — browser warns until you accept it. Enable
          this section to upgrade to a real Let&apos;s Encrypt cert via
          Cloudflare DNS-01 challenge (no browser warning).
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex items-center justify-between rounded-lg border p-3">
          <div>
            <p className="text-sm font-medium">Use Let&apos;s Encrypt cert</p>
            <p className="text-xs text-muted-foreground">
              When on, Caddy issues a real cert via Cloudflare DNS-01 for the
              configured domain. When off, falls back to a self-signed
              internal cert (browser warning until accepted).
            </p>
          </div>
          <Switch
            checked={enabled ?? false}
            onCheckedChange={setEnabled}
            disabled={isPending || enabled === null}
          />
        </div>

        <div className="space-y-3 rounded-lg border p-4">
          <div className="space-y-2">
            <label htmlFor="tls-domain" className="text-xs font-medium">
              Domain
            </label>
            <input
              id="tls-domain"
              ref={domainRef}
              type="text"
              defaultValue=""
              placeholder="hypertrade.example.com"
              className="w-full rounded border bg-background px-3 py-2 text-sm font-mono"
              disabled={isPending}
            />
          </div>
          <div className="space-y-2">
            <label htmlFor="tls-email" className="text-xs font-medium">
              Let&apos;s Encrypt email
            </label>
            <input
              id="tls-email"
              ref={emailRef}
              type="email"
              defaultValue=""
              placeholder="you@example.com"
              className="w-full rounded border bg-background px-3 py-2 text-sm"
              disabled={isPending}
            />
          </div>
          <div className="space-y-2">
            <label htmlFor="tls-cf-token" className="text-xs font-medium">
              Cloudflare API token{" "}
              {cfg.cf_token_set && (
                <span className="text-green-400 ml-1">(currently set — leave blank to keep)</span>
              )}
            </label>
            <input
              id="tls-cf-token"
              ref={tokenRef}
              type="password"
              defaultValue=""
              placeholder={cfg.cf_token_set ? "(unchanged)" : "scoped: Zone:Read + Zone DNS:Edit"}
              className="w-full rounded border bg-background px-3 py-2 text-sm font-mono"
              disabled={isPending}
            />
            <p className="text-xs text-muted-foreground">
              Create at Cloudflare Dashboard → My Profile → API Tokens →
              Custom token. Required permissions:{" "}
              <code className="bg-background px-1 rounded">Zone — Zone — Read</code> and{" "}
              <code className="bg-background px-1 rounded">Zone — DNS — Edit</code>,
              scoped to only the zone serving this domain.
            </p>
          </div>
        </div>

        <div className="space-y-2 rounded-lg border p-3">
          <p className="text-xs font-medium">Caddy status</p>
          {s.reachable ? (
            <>
              <p className="text-xs text-muted-foreground">
                Servers: {s.servers?.join(", ") || "none"}
              </p>
              <p className="text-xs text-muted-foreground">
                Cert subjects: {s.tls_subjects?.join(", ") || "none yet"}
              </p>
            </>
          ) : (
            <p className="text-xs text-yellow-400">
              Caddy admin API unreachable: {s.error || `HTTP ${s.status}`}
            </p>
          )}
        </div>

        {error && (
          <p className="text-sm text-red-400 border border-red-500/30 bg-red-500/5 rounded px-3 py-2">
            {error}
          </p>
        )}
        {savedMsg && (
          <p className="text-sm text-green-400">{savedMsg}</p>
        )}

        <Button onClick={save} disabled={isPending}>
          {isPending ? "Saving…" : "Save & apply"}
        </Button>
      </CardContent>
    </Card>
  );
}
