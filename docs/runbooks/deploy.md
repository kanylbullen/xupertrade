# Deploy (operator)

Operator runbook, moved from `CLAUDE.md` § 3 on 2026-09-24. Every command
here touches the production host; an agent runs one only when the
operator asks for it in the session. Placeholders such as `$DEPLOY_HOST`
follow `CLAUDE.md` § 0 — never commit real values.

**When.** Bot code is deployed once a week, in the bot-deploy window
(for example Tuesdays), so the operator unlocks the tenant bots once
rather than after every merge. Dashboard and Caddy changes are
deployed after merge. Emergency fixes go outside the window. This is
roadmap § 4.1 (`docs/plans/next-level-roadmap.md`); it holds until
deploy-per-SHA (roadmap NA-8) replaces the manual build below.

After a deploy, check the bots with [health-check.md](health-check.md).

## Standard deploy command

**Always split build from `up -d` and verify image age between them.** The
chained one-liner (`build && up -d`) hit the layer cache and silently
produced stale images on three back-to-back PR deploys in a 24h window
(2026-05-12) — the chained command exited 0, dashboard "deployed", and
live behavior matched the pre-PR state because `--force-recreate` then
brought the container up off an unchanged image SHA. See the cache-trap
warning below for the underlying mechanic.

```bash
# 1. Pull master + check disk + build with --no-cache --pull, stamping
#    both images with the commit just checked out (GIT_SHA)
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && \
   git fetch origin && git reset --hard origin/master && \
   df -h / | tail -1 && \
   GIT_SHA=\$(git rev-parse HEAD) phase run -- docker compose --profile build build --no-cache --pull bot-image dashboard"

# 2. Verify the images: both timestamps newer than your commit, and
#    both `rev:` equal to the HEAD printed first
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && echo \"HEAD: \$(git rev-parse HEAD)\" && \
   docker image inspect hypertrade-dashboard -f 'built: {{.Created}}  rev: {{index .Config.Labels \"org.opencontainers.image.revision\"}}' && \
   docker image inspect xupertrade-bot:latest -f 'built: {{.Created}}  rev: {{index .Config.Labels \"org.opencontainers.image.revision\"}}'"

# 3. Only THEN recreate the containers. `--no-deps` is not optional:
#    without it compose also recreates any dependency whose config
#    changed (redis, postgres), and a recreated redis can come up empty.
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && phase run -- docker compose up -d --no-deps --force-recreate dashboard"

# 4. The running dashboard must report the commit you built.
#    /api/version is public, so no session is needed. Right after step 3
#    the dashboard may still be starting; a curl error means retry.
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && want=\$(git rev-parse HEAD) && \
   got=\$(curl -fsS http://127.0.0.1:3000/api/version) && \
   echo \"HEAD:    \$want\" && echo \"running: \$got\" && \
   case \"\$got\" in *\"\$want\"*) echo OK ;; *) echo MISMATCH >&2; exit 1 ;; esac"
```

**The `\$` in `GIT_SHA=\$(git rev-parse HEAD)` is not optional.** The
command must run on the host, after its `git reset`. Unescaped, your
local shell expands it to whatever your own clone has checked out, and
the image then reports a commit it was not built from; nothing checks
the value beyond "looks like hex". Left out entirely, both images build
fine but stamp `unknown`, so `rev: unknown` in step 2 and a `MISMATCH`
in step 4 mean a rebuild with `GIT_SHA`, not a code problem (an empty
`rev:` is an image from before the stamp existed, #182). A
`MISMATCH` with a hex SHA means the dashboard is not running an image
built from HEAD: check step 2 and the cache trap below.

> ⚠️ **Never let compose recreate `redis` by accident.** The dashboard
> `depends_on` redis, so `up -d --force-recreate dashboard` *without*
> `--no-deps` also recreates redis whenever redis's compose config
> changed. PR #168 changed it (named `redisdata:/data` volume), and the
> first recreate after that starts on an **empty** volume. Redis holds
> the paused flags, the per-mode `disabled` strategy sets, leverage
> overrides, the per-bot API keys, the session secret, the auth config,
> the OIDC client secret and the CF token. An empty Redis reads as
> "not paused, nothing disabled", so every bot — mainnet included —
> resumes with every strategy enabled (auth itself fails closed to
> `locked`). Before any deploy that recreates redis: pause the bots,
> `docker exec hypertrade-redis-1 redis-cli SAVE`, `docker cp` the
> `dump.rdb` out, copy it into the target volume, and only then
> recreate redis on its own and check `DBSIZE` and the key list match.

> ⚠️ **`up -d --force-recreate dashboard` only refreshes the dashboard
> container.** Tenant-bots are orchestrator-spawned (separate Docker
> containers, not in `docker-compose.yml` post-PR-4c) and the orchestrator
> does NOT auto-restart them when `xupertrade-bot:latest` updates. So a
> bot code change ships the new image into the registry but already-running
> tenant bots keep using the read-only layers from the image they spawned
> against until the dashboard explicitly recreates them.
>
> When the bot code itself changed (not just the dashboard), the
> operator must trigger a tenant-bot restart per running bot via the
> dashboard UI's `Settings → Bots → restart`. Or via API:
> `POST /api/tenant/me/bots/<bot_id>/stop` then `/start`. Until that
> happens, the new bot logic only applies to bots STARTED after the
> deploy — not to ones already running.

**Checking which build a bot runs.** `/settings/bots` shows the
dashboard's build and each running bot's, as `<short sha>, built <time>`.
A bot restarted after the deploy must show HEAD; one that wasn't
restarted still shows the build it was started on. `unknown` on a bot
card means its image has no stamp: either the bot was started on an
image from before the stamp (its `/api/version` answers 404) and needs a
restart, or the image was built without `GIT_SHA` and needs step 1
again. A bot's `/api/version` needs no API key, so from the host:
`docker exec <bot-container> python -c 'import urllib.request; print(urllib.request.urlopen("http://localhost:<port>/api/version").read().decode())'`
(ports as in [health-check.md](health-check.md)).

**There is no per-mode compose profile or `bot-mainnet`/`bot-testnet`
service any more** (that model predates the multi-tenant orchestrator, see
`CLAUDE.md` § 2). One `bot-image` build (profile `build`) produces
`xupertrade-bot:latest` for every mode; step 1 above (`--profile build
build --no-cache --pull bot-image dashboard`) already covers mainnet bots
too. To pick up new bot code, restart the running mainnet bot the same way
as any other tenant bot: dashboard UI `Settings → Bots → restart`, or
`POST /api/tenant/me/bots/<bot_id>/stop` then `/start`.

> ⚠️ **Cache trap on first build of a service that hasn't been built
> recently.** An old image with the same `name:tag` from a long-stopped
> previous build can silently win the layer cache, and a plain `docker
> compose build <service>` does NOT invalidate it — the build "succeeds"
> in seconds with zero COPY-step output but the resulting image is
> days/weeks old and missing your latest code. This bit us on the first
> mainnet boot 2026-05-10 (back when mainnet ran as its own compose
> service, `bot-mainnet`, before the orchestrator refactor): the image
> lacked the audit-C3 allowlist and all 21 strategies traded on real money
> for one tick before pause caught it. **Always force `--no-cache` on the
> first build of any service/image that hasn't been built recently:**
>
> ```bash
> cd /opt/hypertrade && GIT_SHA=$(git rev-parse HEAD) \
>   phase run -- bash -c 'docker compose --profile build build --no-cache --pull bot-image'
> ```
>
> (`--pull` refreshes the base image too — combining both ensures the
> entire image is rebuilt from current sources.)
>
> When in doubt, look at `docker images | grep xupertrade-bot` — if the
> "CREATED" column says days/weeks ago, force a no-cache rebuild before
> restarting any bot off it.
>
> **This trap is not bot-image-specific** — it bit the dashboard the
> same way on 2026-05-11 during the healthcheck-fix iterations. Five
> back-to-back PRs rebuilt dashboard but `docker compose build --pull
> dashboard` hit the layer cache every time and never produced a new
> image SHA.
> `--force-recreate` then recreated the container off the same stale
> image, so the new code never reached production. Symptom: deploy
> command exits 0, container restarts, but live behavior matches the
> pre-PR state. After three rounds of "but I deployed it!" confusion,
> `docker image inspect hypertrade-dashboard -f '{{.Created}}'` showed
> the image was 2 days old. **Verify image age after every deploy when
> you change the dashboard or bot code:**
>
> ```bash
> docker image inspect hypertrade-dashboard -f 'built: {{.Created}}'
> ```
>
> If the timestamp pre-dates your commit, force a no-cache rebuild.
>
> **Disk-full mid-build is the silent corollary.** The 30G root partition
> fills up after ~5–10 dashboard rebuilds because docker's builder cache
> grows unbounded. When the disk fills mid-COPY, `docker compose build`
> emits a confusing error and may still exit 0 in the chained
> `build && up -d` form, leaving a half-baked or missing image. Run
> `docker builder prune -af` before deploying when `df -h /` shows the
> root partition above 80%. A daily host-side cron (see
> "Host-side cron jobs" below) keeps this from accumulating between
> deploys.

Build only what changed for speed — but keep `--no-cache --pull` and
the standalone build step (no chained `&& up -d`):

```bash
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "cd /opt/hypertrade && GIT_SHA=\$(git rev-parse HEAD) phase run -- docker compose --profile build build --no-cache --pull dashboard"
```

Then verify the image, recreate and check `/api/version` per the steps
above. The
old `bash -c 'build --pull && up -d'` form is what bit us with the
cache-trap — don't reintroduce it.

## Host-side cron jobs

These are installed manually on `$DEPLOY_HOST` (root crontab); they live
on the host, NOT in the repo. Documented here so a future agent knows
what's expected to be running and why.

| Schedule | Command | Why |
|---|---|---|
| `0 4 * * *` | `docker builder prune -af > /var/log/docker-prune.log 2>&1` | Docker's builder cache grows unbounded between deploys and fills the 30G root partition after ~5–10 dashboard rebuilds. Without this, deploys eventually fail with disk-full errors mid-COPY (see the cache-trap warning above). 04:00 UTC is well outside trading-hours volatility windows. |

To install:

```bash
ssh -i ~/.ssh/hypertrade root@$DEPLOY_HOST \
  "( crontab -l 2>/dev/null; echo '0 4 * * * docker builder prune -af > /var/log/docker-prune.log 2>&1' ) | crontab -"
```

To verify after install: `ssh root@$DEPLOY_HOST 'crontab -l'`.

**Re-verify after every host rebuild, not just after install.** The root
crontab lives only on the host, not in any Docker volume or the repo — a
host rebuild (new VM, disk restore, provider migration) silently drops it.
Found missing entirely on 2026-09-15 (disk had climbed to 83% with 10+ GB
reclaimable and no prune had run). `crontab -l` costs one SSH round-trip;
run it as a standing item whenever you touch the host, not only right after
you install the job.
