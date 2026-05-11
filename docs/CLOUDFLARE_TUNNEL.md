# Cloudflare Tunnel — public access for closed-beta

`hypertrade` runs behind Caddy on the operator's host for LAN
access. For closed-beta we need invited users to reach the
dashboard from the public internet without:

- Opening inbound ports on the host firewall
- Pointing a public DNS A-record at the operator's home IP
- Relying on a third-party VPS as a reverse proxy

**Cloudflare Tunnel** solves this: cloudflared connects OUT from
the host to Cloudflare's edge, accepts forwarded HTTP requests
over that tunnel, and routes them to `dashboard:3000` on the
internal docker network. CF terminates public TLS; Caddy stays
in place for LAN access.

The tunnel is gated behind a docker-compose profile (`public`) so
LAN-only deploys don't need a Cloudflare account.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│ Public user                                                      │
│   browser → https://hypertrade.example.com                        │
└─────────────────────────────┬────────────────────────────────────┘
                              │ TLS terminated at CF edge
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Cloudflare edge (DDoS, bot-mgmt, optional Access policy)        │
└─────────────────────────────┬────────────────────────────────────┘
                              │ private tunnel (no inbound ports)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ Operator's host                                                  │
│   cloudflared (docker) ── outbound persistent connection        │
│      │                                                           │
│      │ HTTP over docker network                                  │
│      ▼                                                           │
│   dashboard:3000 → Authentik OIDC → Postgres / Redis / bots     │
└─────────────────────────────────────────────────────────────────┘

LAN users still reach the dashboard via Caddy on the host's :443
(Let's Encrypt cert, internal DNS). Caddy and the tunnel coexist —
each serves a different audience.
```

---

## Operator setup (one-time)

### 1. Create the tunnel in Cloudflare Zero Trust

1. Sign in to [one.dash.cloudflare.com](https://one.dash.cloudflare.com)
   (Zero Trust dashboard).
2. **Networks → Tunnels → Create a tunnel**.
3. Connector: **Cloudflared** → tunnel name e.g.
   `hypertrade-prod`.
4. Cloudflare shows a **token** (a long base64-ish string) and a
   `docker run` example. Copy the token only — we run cloudflared
   via compose, not the example one-liner.

### 2. Configure public hostname routing

Still in the Tunnel setup wizard:

1. **Public Hostnames → Add a public hostname**
2. Subdomain: `hypertrade` (or whatever you want)
3. Domain: pick the operator's domain (e.g. `example.com`)
4. Service: type `HTTP`, URL `dashboard:3000`
   (the docker-network DNS name — cloudflared and dashboard share
   the default docker-compose network)
5. Save. Cloudflare automatically creates a CNAME record from
   `hypertrade.<domain>` to the tunnel.

If you want the tunnel hostname to match an existing record
(`hypertrade.example.com`, currently A-record on LAN IP for Caddy),
either:

- Replace the A-record with the CF-managed CNAME — public users
  go through tunnel, LAN users lose direct Caddy access (or rely
  on `/etc/hosts` overrides on their own machine)
- Use a separate hostname for public traffic
  (`pub.hypertrade.example.com`) and keep the LAN A-record on the
  original — this is the cleanest split

### 3. Store the token in Phase

```bash
# Locally, against the operator's Phase instance
phase secrets create CLOUDFLARE_TUNNEL_TOKEN=<token-from-step-1>
```

The compose file reads `${CLOUDFLARE_TUNNEL_TOKEN}` from
process env; Phase injects it via `phase run -- docker compose ...`.

### 4. Enable the `public` profile + start cloudflared

On the deploy host:

```bash
ssh root@$DEPLOY_HOST
cd /opt/hypertrade
git fetch origin && git reset --hard origin/master   # pull this PR
phase run -- docker compose --profile public up -d cloudflared
```

Verify the tunnel is connected:

```bash
docker logs hypertrade-cloudflared --since 1m | grep -E 'connection|tunnel'
# Expect: "Connection registered" / "Updated to new configuration"
```

### 5. Test public reachability

From OUTSIDE the LAN (e.g. cellular hotspot on your phone):

```
curl -v https://hypertrade.example.com/api/auth/config
```

The dashboard exposes `/api/auth/config` without auth so the login
page can decide which auth modes to render. A successful response
proves:

- TLS handshake succeeds (CF cert)
- CF Tunnel routes to the dashboard
- The dashboard is up and responsive

```
curl -v https://hypertrade.example.com/login
```

`/login` is also unauthenticated and returns the login page HTML.
Either is a good public-reachability smoke test.

If you get a redirect to `/login` from another path, that's also
a healthy sign — confirms public reachability + auth gate working
on a protected route.

---

## Operations

### Restart cloudflared

```bash
phase run -- docker compose --profile public restart cloudflared
```

### Rotate the tunnel token

If you suspect the token was leaked:

1. CF Zero Trust → tunnel → **Refresh token** (or delete + create new)
2. Update Phase: `phase secrets update CLOUDFLARE_TUNNEL_TOKEN=<new>`
3. Restart cloudflared (above)

The old token stops working immediately on rotation.

### Disable public access temporarily

```bash
docker compose stop cloudflared
```

Or stop the tunnel from the Cloudflare side (Zero Trust → tunnel →
Disable). LAN access via Caddy is unaffected.

### Deploy without public access

LAN-only deploys skip the `public` profile entirely:

```bash
phase run -- docker compose up -d  # no --profile public → no cloudflared
```

This is the right mode for solo-operator (Tier 1) deploys per
[`SECURITY.md`](../SECURITY.md).

---

## Threat model addendum

CF Tunnel adds a new trust dependency — Cloudflare. Implications:

### What changes for the public path

- **TLS termination at CF edge.** CF sees decrypted traffic
  (request headers, body, response). Same as any reverse proxy.
- **Dependency on CF availability.** If CF Tunnel has an outage,
  public users can't sign in. LAN users (via Caddy) unaffected.
- **CF can log requests.** Standard CF analytics; review their
  data-retention policy.

### Existing protections still apply

- Authentik OIDC still required to access non-public dashboard
  routes (proxy.ts gate)
- Per-tenant K-cache + RLS still enforce isolation BEHIND auth
- Operator's `API_KEY` still required for
  `/api/control/*` (POST), `/api/admin/*` (planned)

### Optional hardening: Cloudflare Access

CF Access can add an auth gate on TOP of Authentik (one-time
email codes, GitHub OAuth, etc). For closed-beta this is overkill
(Authentik group membership already gates registration), but
useful if:

- You want to revoke a tenant's public access without removing
  them from Authentik
- You want time-bound access (e.g. 7-day invite code)
- You want IP-based allowlist as a secondary gate

To enable later, add an Access application in CF Zero Trust →
Access → Applications → Add → Self-hosted, host
`hypertrade.<domain>`, configure policies. No code change on
this side; Access intercepts BEFORE the tunnel forwards traffic.

---

## Troubleshooting

### `cloudflared` exits with "missing tunnel token"

- `CLOUDFLARE_TUNNEL_TOKEN` not set or empty in the env Phase
  injects. Verify with:
  ```bash
  phase run -- bash -c 'echo "len=${#CLOUDFLARE_TUNNEL_TOKEN}"'
  ```
  Should print `len=212` or similar (CF tokens are ~200 chars).

### Tunnel connects but dashboard returns 502 Bad Gateway

- cloudflared can't reach `dashboard:3000` over docker network.
  Likely the dashboard container is down or unhealthy:
  ```bash
  docker compose ps dashboard
  docker logs hypertrade-dashboard | tail
  ```

### Public hostname returns CF "1033 Argo Tunnel error"

- Tunnel registered but no public hostname configured. CF Zero
  Trust → tunnel → Public Hostnames tab → add the route.

### Public hostname returns NXDOMAIN

- CF didn't create the CNAME (DNS propagation, or the domain
  isn't on Cloudflare). Verify:
  ```bash
  dig hypertrade.example.com
  # should resolve to <tunnel-id>.cfargotunnel.com
  ```

---

## Phase 8 checklist update

The closed-beta launch in
[`INVITE_ONBOARDING.md`](INVITE_ONBOARDING.md) now requires:

- [ ] Cloudflare Tunnel running (`docker compose --profile public
      ps cloudflared` shows `Up`). The container has no Compose
      healthcheck defined — verify health from the CF Zero Trust
      dashboard (tunnel status: Healthy) or
      `docker logs hypertrade-cloudflared | grep "Connection registered"`.
- [ ] `https://<your-public-hostname>/` reachable from outside the
      LAN
- [ ] First test sign-in by a tenant from outside the LAN works
      end-to-end
