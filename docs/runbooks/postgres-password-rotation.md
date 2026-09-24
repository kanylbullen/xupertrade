# Rotating POSTGRES_PASSWORD

Operator runbook, moved from `CLAUDE.md` § 3 on 2026-09-24. Every command
here touches the production host; an agent runs one only when the
operator asks for it in the session. Placeholders such as `$DEPLOY_HOST`
follow `CLAUDE.md` § 0 — never commit real values.

**Preferred path: `scripts/rotate-postgres-password.sh`** (PR #124). It
does step 3 idempotently: it first checks over TCP whether the Phase
value already authenticates, and only then runs `ALTER USER` through
`docker exec … psql`, which uses the container's socket and so does not
need the old password (see the script header for why the TCP check
matters). Its usage block lists the full sequence. The manual steps
below are the same dance by hand.

`POSTGRES_PASSWORD` lives in Phase (app `xupertrade`, env `Development`).
Postgres only reads that env on **container init** — updating it in Phase
does NOT change the password inside an already-initialized database. Full
rotation is therefore a two-step dance: update the live DB user with
`ALTER USER` (over the container's socket, which `pg_hba.conf` trusts, so
no password is needed), then recreate every container that has a
`DATABASE_URL` baked into its env so they pick up the new value.

> `docker-compose.yml` has required `POSTGRES_PASSWORD` via
> `${POSTGRES_PASSWORD:?...}` (compose-up fails fast if it's unset/empty)
> since PR #122 — both the postgres and dashboard services read the
> Phase-injected value, no hardcoded literal survives a `phase run --`
> deploy. The steps below are the live rotation dance.
>
> Tenant-bot containers spawned by the dashboard read `DATABASE_URL` from
> the dashboard's environment, so they inherit whatever the dashboard
> sees at spawn time.

Steps:

1. **Generate a fresh secret locally** (don't commit, don't paste in chat):
   ```bash
   openssl rand -base64 36 | tr -d '/+=\n'
   ```

2. **Update Phase**: web UI → app `xupertrade` → env `Development` → key
   `POSTGRES_PASSWORD` → save.

3. **ALTER inside the running postgres container.** `docker exec … psql`
   connects over the socket, which `pg_hba.conf` trusts, so it works
   without the old password — and so it proves nothing about the password
   either (the script's TCP check exists for that). Unlike the script, this
   form puts the new password in argv, visible to `ps` on the host:
   ```bash
   ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
     "docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade \
      -c \"ALTER USER postgres WITH PASSWORD '<paste new>';\""
   ```

4. **Recreate consumers** so they pick up the new env-injected
   `DATABASE_URL`:
   ```bash
   ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
     "cd /opt/hypertrade && phase run -- docker compose up -d --no-deps --force-recreate dashboard"
   ```
   Each tenant bot must be Stop+Started via the dashboard UI
   (Settings → Bots → restart) so its container is respawned with the
   new env. Until that happens, already-running tenant bots keep using
   the OLD `DATABASE_URL` from their spawn-time env and will start
   failing auth on the next reconnect.

5. **Verify**:
   ```bash
   ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
     "docker exec hypertrade-postgres-1 psql -U postgres -d hypertrade -c '\\du'"
   ```
   Should list the `postgres` role without auth error. Spot-check that
   the dashboard `/` and a tenant bot's `/api/positions` still respond.

The postgres CONTAINER itself does NOT need to be recreated — the user
password lives in `pg_authid` (durable WAL state) and `ALTER USER`
updates it in place. Recreating postgres would mean a
volume-preserving down/up, which is more disruption than needed and
risks DB downtime during active trading.
