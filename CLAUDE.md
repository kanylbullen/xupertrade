# HyperTrade — Agent Development Framework

The operating manual for any Claude agent in this repo (Cline and Codex
start from [AGENTS.md](AGENTS.md), which points here). The mission: a
**production-grade autonomous crypto trader** that executes its 22
registered strategies faithfully, recovers from failure, and never silently
diverges from exchange reality. The agent works on its own up to a reviewed
PR; the operator merges and deploys. It stops to ask when an action is
destructive or genuinely ambiguous.

Elsewhere, to keep this file short: fixed-backlog history in
[docs/CHANGELOG.md](docs/CHANGELOG.md), operator runbooks in
[docs/runbooks/](docs/runbooks/), the plan in
`docs/plans/next-level-roadmap.md` (PR #177 until merged).

---

## 0. Personal info & secrets policy — READ EVERY COMMIT

**This repository is public.** Every commit goes to GitHub where anyone can
read it forever — even if you delete the file in a later commit, the value
lives in history. **Never commit any of these:**

| Type | Example | Where it goes instead |
|---|---|---|
| Telegram bot token | `8639592584:AAGj…` (digits, colon, 35 base64 chars) | Phase secrets manager (see § 3) |
| HyperLiquid private key | `0x` + 64 hex chars | Phase secrets manager |
| Cloudflare API token | 40-char base64 | Redis (`dashboard:tls:cf_token`) via operator-only `POST /api/tls/configure`. `lib/tls-config.ts` reads `TLS_CF_API_TOKEN` first, but `docker-compose.yml` doesn't pass it to the dashboard today |
| OIDC client secret | provider-specific | Phase (`OIDC_CLIENT_SECRET`), copied into Redis (`dashboard:auth:oidc:client_secret`) at start |
| `API_KEY` for the bot HTTP API | random string | Phase secrets manager |
| Phase service token | base64 | only on the host's `~/.phase/` config; NEVER in repo |
| Personal email used live | `you@yourdomain.com` | Phase (`TELEGRAM_*`, OIDC config); generic placeholder in docs (`you@example.com`) |
| Telegram chat ID | 8-12 digit number used as your identity | Phase secrets manager |
| Server IP / private LAN | `192.168.x.x`, `10.x.x.x` | `~/.ssh/config`, env vars (`$DEPLOY_HOST`, `$DEPLOY_IP`) |
| Real production hostname | `mybot.example.com` | local env / SSH config; use `$DEPLOY_HOST` placeholder in docs |
| Phase / secrets-manager URL | `secrets.example.com` | local env; use `$PHASE_URL` placeholder in docs |
| Wallet addresses (HL trading account) | `0x` + 40 hex | not needed in repo; bot reads from Phase |
| Holdings / position sizes that identify you | "I have 400 VVV" in commit msg | discuss in chat, not in commits |

**Before EVERY commit**, a pre-commit hook (see § Setup) blocks the diff
when known secret-shaped strings appear. **Never bypass with `--no-verify`**
unless the match is genuinely a false positive AND you've manually verified
the value isn't sensitive.

**Even with the hook, you (or a future agent) are still responsible.** The
hook catches known patterns, not novel ones. When writing docs, prefer:

- `$DEPLOY_HOST`, `$DEPLOY_IP`, `$YOUR_DOMAIN` placeholders
- `you@example.com`, `1234567890:your-actual-token-here` for examples
- `~/.ssh/<keyname>` for SSH key paths (not `/home/<user>/.ssh/...`)

**If you discover a secret that's already in history:**
1. **Rotate the credential immediately** (Telegram: `/revoke` to BotFather; HL: regenerate API wallet; CF: revoke the token in CF dashboard).
2. Mask the value in `HEAD` and commit + push the mask.
3. Optionally: rewrite history with `git filter-repo --replace-text` to scrub from older commits. Destructive — coordinate before doing it. Even then, anyone who already cloned has the old value.
4. Add the leaked pattern to the pre-commit hook's blocklist so it can't recur.

### Setup the hook (one-time, per local clone)

```bash
git config --local core.hooksPath .githooks
chmod +x .githooks/pre-commit
```

The hook is checked into `.githooks/pre-commit`. Setup is per-clone because
git ignores `core.hooksPath` from a checked-in `.git/config`.

---

## 1. Mission and definition of done

**Mission:** Build and maintain a HyperLiquid autotrader that
1. Executes the 22 registered strategies with byte-fidelity to their TradingView ports (where a port exists — `vvv_hedge` and `ath_breakout` are in-house designs, not ports).
2. Survives network outages, exchange errors, DB hiccups, and process restarts without losing position state.
3. Produces accurate trade records, equity snapshots, and PnL — DB always matches exchange reality.
4. Surfaces problems via Telegram and the dashboard before they become silent losses.

**The agent cannot guarantee a strategy is profitable.** It *must* keep the
logic identical to the Pine source, detect demonstrably broken strategies
(SL=0, sign flips, unit confusion) and propose their removal, and recommend
disabling strategies whose live behavior diverges from their backtest
archetype, with evidence.

**Definition of done: PR → review → merge by the operator.**
1. A feature branch cut from `origin/master`, committed (§ 7), pushed, with a PR open against `master`.
2. Green gates for what it touches. Bot: `cd bot && uv run pytest -q`. Dashboard: `cd dashboard && npm ci && npx tsc --noEmit && npm run lint && npm test && npm run build`.
3. A fixed bug has a test or runtime check that would have caught it.
4. A review pass happened; every finding is fixed or answered (§ 7).
5. The operator merges. An agent merges only when the operator asks, for that PR.
6. The operator deploys ([deploy](docs/runbooks/deploy.md)): dashboard and Caddy after merge, bot code in the weekly bot-deploy window (roadmap § 4.1), emergency fixes outside it. Then live verification ([health check](docs/runbooks/health-check.md)); an agent runs it only when asked.

---

## 2. Repo layout (the parts that matter)

```
bot/hypertrade/
├── main.py             # entry; runs every registered strategy. Testnet/mainnet refuse to boot without a DB
├── config.py           # pydantic-settings; unknown env vars ignored
├── api.py              # aiohttp API: control, positions, indicator-status, hodl, vaults, HL diagnostic
├── engine/             # runner.py (tick loop, reconcile 5 min, funding poll 30 min, flip-detect, open gates),
│                       # control.py (Redis flags), portfolio.py (kill switch, daily PnL), indicators_status.py
├── exchange/           # base.py, paper.py (simulated), hyperliquid.py (SDK; reads retry, writes never)
├── strategies/         # 22 registered (registry.py), meta/<name>.json; golden_cross.py deliberately unregistered
├── hodl/, vaults/      # advisory spot signals, vault scanner: read-only, no orders
├── reconcile/fills.py  # prices reconcile closes from HL fills
├── data/, events/      # candle feed with retry; Redis pub/sub bus
├── notify/telegram.py  # notifier + command bot
├── reports/, backtest/ # weekly_eval.py (/eval, /kelly); backtest CLI + metrics
└── db/                 # models.py, repo.py (all SQL, reconcile_positions)
bot/tests/, bot/alembic/versions/ (0001_initial_schema → 0016_tenant_admin_limits)
dashboard/src/          # Next.js 16: app/ (pages + /api), proxy.ts (auth gate), components/, lib/
  lib/bot-orchestrator.ts, docker.ts  # spawn bot containers via the host Docker socket
  lib/bot-api.ts, bot-api-key.ts      # proxy to a bot with its per-bot X-Api-Key
  lib/auth-config.ts, oidc.ts, safe-next.ts, phase-sync.ts, caddy-admin.ts, heartbeat-watchdog.ts
caddy/, tv-source/<name>.pine, scripts/, docs/
docker-compose.yml      # postgres, redis, dashboard, caddy, cloudflared (profile public), bot-image (profile build)
```

**No bot runs from `docker-compose.yml`**; `bot-image` only builds
`xupertrade-bot:latest`. The orchestrator turns a `tenant_bots` row plus the
tenant's decrypted secrets into one container per tenant per mode
(`xupertrade-bot-<16-hex id>-<mode>`, no published ports,
`restart: unless-stopped`). Only the mainnet bot gets
`TELEGRAM_ENABLED=true`, so Telegram runs there.

---

## 3. Operating environment

- **Local:** `~/xupertrade/`. **Server:** `root@$DEPLOY_HOST`, code in `/opt/hypertrade/`, key `~/.ssh/hypertrade`.
- **Git:** `https://github.com/kanylbullen/xupertrade.git`, branch `master`. Ruleset `default-protection` requires a PR for `master`, blocks force-push and deletion, and has no bypass actors.
- **Postgres** `127.0.0.1:5432`, **Redis** `127.0.0.1:6379`: loopback only. Redis has no `requirepass`, so reachability *is* its access control; it holds the session-signing secret, the OIDC secret, the CF token and the per-bot API keys. **Never publish it beyond loopback without `requirepass`** threaded through dashboard and bots.
- **Bot APIs:** paper `:8000`, testnet `:8001`, mainnet `:8002`, inside the containers only; most routes need the bot's `X-Api-Key`.
- **Dashboard:** `127.0.0.1:3000` for probes; users come through Caddy (`:443`, LAN) or the Cloudflare tunnel. **Caddy:** `:80` → HTTPS, `:443`, `:443/udp`; admin API `:2019` internal only. TLS: Let's Encrypt via Cloudflare DNS-01, else self-signed for `CADDY_HOST`.
- **Auth:** basic or OIDC via Phase (`AUTH_MODE`, `OIDC_*`, copied to Redis at boot by `lib/phase-sync.ts`) or `scripts/set-basic-auth.sh`; no settings UI. Stored mode gone and no config left → `locked`.
- **Secrets:** in Phase, injected by `phase run --`; no `.env` on the host.

**Runbooks** (the operator's; an agent runs one only when asked):

| Runbook | For |
|---|---|
| [deploy](docs/runbooks/deploy.md) | build, image-age check, recreate, cache trap, Redis hazard, bot restarts, prune cron |
| [health-check](docs/runbooks/health-check.md) | DB ↔ exchange parity, a bot's API, logs when `docker logs` breaks |
| [dashboard-auth-recovery](docs/runbooks/dashboard-auth-recovery.md) | "Authentication is locked" on `/login` |
| [postgres-password-rotation](docs/runbooks/postgres-password-rotation.md) | rotating `POSTGRES_PASSWORD` |
| [phase-secrets](docs/runbooks/phase-secrets.md) | adding a secret, Phase on a new host |

---

## 4. Subagents

Model choice and delegation follow the workspace policy in `~/CLAUDE.md`
(one level above this repo, not in git), section *"Offloada arbete till
subagenter på Opus och Sonnet"*. It decides; this repo has no model table.
On top of it (roadmap § 4.1): the order path, live-DB migrations,
production backfills, secrets and production Redis stay with Opus or the
main session, never sonnet or Kanban.

---

## 5. Backlog

Open items only. The PR that fixes one moves its entry verbatim to
[docs/CHANGELOG.md](docs/CHANGELOG.md), ticked and citing the PR number.
Never delete an entry. Add one for a defect you find and don't fix now.
Planned initiatives live in the roadmap.

### Open — Critical (blocks safe operation)

(none currently)

### Open — High (impacts trading correctness)

(none currently)

### Open — Medium

- [ ] **Volatility-adjusted sizing (option C from Kelly discussion).** Replace fixed `MAX_POSITION_SIZE_USD` with ATR-normalized sizing: `notional = RISK_BUDGET_USD / (atr × atr_mult)` so every trade has roughly the same dollar-risk regardless of asset volatility. Industry standard, no statistical estimation needed. Add `RISK_BUDGET_USD` config; keep `MAX_POSITION_SIZE_USD` as a hard cap. ~3-4h work, defensive change. Pair with the Kelly report for guidance on the budget level. *(Roadmap SE-2.)*
- [ ] **Drawdown-based auto-scaling (option B from Kelly discussion).** Add `MAX_STRATEGY_DRAWDOWN_PCT` per strategy. When 80% of cap reached → halve effective margin until 7-day rolling PnL > 0. Limits exposure on degrading strategies without requiring stationary distribution assumptions like Kelly does. *(Roadmap SE-2.)*

### Open — Low

(none currently)

---

## 6. Working principles

**Investigation before code.** Reproduce or observe the problem (log line,
DB row, screenshot), read the relevant code top to bottom, then write the
fix and a check that would have caught it. If a fix is "obvious" without
the first step, it is probably wrong.

**Keep DB and exchange in lockstep** — divergence is the most dangerous
failure here, and we have lived through it. Code that opens or closes
positions writes to the DB **before** the order (or records the order ID
and reconciles), tolerates an order that succeeds while the DB write fails,
and is idempotent on retry. Reconcile is the safety net, not the strategy.

**No features without a need.** Resist more strategies, pages and
abstractions; the backlog and the roadmap are the plan.

**Logs are the API.** Every open, close, skip, reconcile and error logs
strategy, symbol, side, size, price and the *reason*.

**Telegram is for humans:** `trade.executed`, `position.closed`, `error`
(plus vault and HODL state changes); never `signal.generated` or tick noise.

**Money:** close sizes come from `position.size` in the DB, never
recomputed; never compare float prices with `==` (use `abs(a - b) < 1e-6`);
fees are subtracted at trade-record time; `notional = margin * leverage`,
and stop distances are on price.

**Testing.** Every registered strategy has a test class in
`bot/tests/test_strategies/` (`vvv_hedge` in `bot/tests/test_vvv_hedge.py`)
covering warmup guard, entry signal, restore without instant close and SL
exit; new strategies add one in that shape. `test_universal_invariants.py`
property-tests the whole registry. `uv run pytest -q` in `bot/`: 1188
passed, 1 skipped, 3 xfailed on 2026-09-24. Paper mode is the integration
test.

---

## 7. Workflow per task

1. State the task in one sentence; check § 5 and the roadmap.
2. Investigate: code, logs, DB rows (read-only).
3. Non-trivial work: a plan in `docs/plans/<feature>.md`, signed off by the operator before coding.
4. `git fetch origin && git switch -c <type>/<short-name> origin/master`. Types `feat`, `fix`, `docs`, `refactor`, `chore`; kebab-case and specific (`feat/vault-scanner`).
5. Stay within the PR budget in [AGENTS.md](AGENTS.md): ~600 added non-test lines, mechanical refactors apart, one open order-path PR at a time.
6. Gates (§ 1), commit, `git push -u origin <branch>`, `gh pr create --base master`. PR body: `## Summary` (what, why), `## Test plan` (gates, checks), `## Notes for reviewer` (subtleties, follow-ups).
7. Review (below); fix or answer every finding.
8. The operator merges (`gh pr merge --squash --delete-branch`) and deploys (§ 1).

Commit messages: `<type>: <imperative summary, <72 chars>`, a WHY
paragraph, and `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.

**No direct pushes to `master`.** The ruleset refuses them, and the
PreToolUse hook `.claude/hooks/guard_bash.py` blocks them locally, plus
`--no-verify`, `git commit -n` and `git -c core.hooksPath=…`. An emergency
fix is a PR the operator merges without waiting for review; a broken
master commit is undone by a `git revert` PR, never a force-push. The
overrides `XUPERTRADE_ALLOW_MASTER_PUSH=1` / `XUPERTRADE_ALLOW_NO_VERIFY=1`
are read from the environment Claude Code started with, so only the operator
can set them. Hook tests: `python3 .claude/hooks/test_guard_bash.py`.

### Review before merge — there is no automated code reviewer

Every PR runs `gitleaks` (`.github/workflows/secret-scan.yml`) and CodeQL
default setup. Both are security scans; nothing on GitHub reviews code.
Copilot review stopped after PR #134; reading its silence as approval once
merged five unreviewed PRs (#156–#160).

**A PR with no review is unreviewed, not approved.** Before merge, one holds:
1. A human reviewed the diff; every comment is fixed or answered.
2. An agent review ran, and every finding was classified (bug / nit / spurious), fixed or answered, and noted in the PR. `/review` uses 3–4 angles on a small diff and all eight on the order path, money or migrations; `.github/review-team/` has 11 repo-native lenses.
3. The operator waived review for a trivial change.

No review available → leave the PR open. Green gates prove the tests pass,
not that the code is right. Reply inline with
`gh api -X POST repos/<owner>/<repo>/pulls/<N>/comments/<id>/replies -f body="..."`.

### When to ask the user vs. just do it

**Just do it (in a PR):** code fixes, refactors, tests; logging, retry and
reconcile improvements; docs, `.env.example`, README; features that fit the
existing architecture; recommending a strategy disable, with evidence.

**Ask first (plan in `docs/plans/`):** merging or deploying; the production
host, DB rows or Redis; disabling a strategy in live config; real funds or
mainnet trading; `git reset --hard` on shared branches or force-push; an
architecture change touching >5 files; removing strategies, columns or
endpoints (vs. deprecating); features that don't fit the architecture (e.g.
the vault scanner). When in doubt, write the plan first.

---

## 8. Strategy evaluation policy

Weekly (Sunday) or on demand: take each strategy's last 7 days of
`mode='testnet'` trades; compute trades, win rate, realized PnL, average PnL
per trade and max consecutive losses; compare with its archetype (mean
reversion ~50%+ win rate, momentum <40% with bigger wins); flag 0 trades
for 14+ days, realized PnL more than 2σ below the backtest expectation, and
gross-positive but net-negative after fees and funding.
`hypertrade.reports.weekly_eval` computes it and posts to Telegram (Sunday
18:00, or `/eval [days]`) and stdout; it writes no file. Durable write-ups
go by hand under `bot/reports/`.

Testnet is a pipe test for execution, not evidence of edge (roadmap
decision 5.6). The agent **recommends** disable, the user **decides**,
through the dashboard `/options` page or
`POST /api/control/strategy/{name}/toggle`, never by editing strategy code.

---

## 9. Common pitfalls — read before debugging

- **`asyncio.to_thread` + `asyncio.run` deadlock with asyncpg in Python 3.13.** This burned us with Alembic. Don't call `asyncio.run` from inside a thread that's already in an event loop. Use `loop.run_in_executor` instead, or run blocking code in a real subprocess.
- **HyperLiquid position is netted per-coin.** If two strategies open opposing sides on the same coin, the exchange shows the net. The DB will diverge unless `allow_multi_coin=False` is enforced (which it now is by default).
- **HyperLiquid sizes have per-asset `szDecimals`.** BTC is 5 decimals, SOL is 2. Round before submitting orders or you get cryptic 422s. The exchange wrapper already does this — don't bypass it.
- **HyperLiquid prices have a 5-significant-figures rule.** Same wrapper handles it.
- **Telegram HTML parse mode breaks on raw `<` and `>`** in dynamic strings. Always `html.escape()` user-supplied or model-supplied text. Already wrapped in `notify/telegram.py`.
- **`docker compose exec -T`** is required for non-TTY commands over SSH. Forgetting `-T` makes the command hang.
- **Postgres `is_open == True` in SQLAlchemy** must be `== True` (not `is True` or just `is_open`). Drizzle and SQLAlchemy both lift `True` to `1` in the SQL but only with explicit comparison.
- **Settings are loaded once at process start.** Changing `.env` requires a container restart. Runtime overrides go through Redis (`BotControl`), not env vars.
- **`req.url` inside Docker returns the container's bound hostname**, not the public hostname users see through the reverse proxy. Any redirect built from `req.url` (NextResponse.redirect, OIDC redirect_uri, login redirects) will send users to `https://<docker-id>:3000/`. Always resolve through `PUBLIC_URL` (or `DASHBOARD_URL` as fallback). Pattern: `lib/oidc.ts:resolveRedirectUri()`, `proxy.ts` login fallback, `app/api/auth/oidc/callback/route.ts:publicLoginUrl()`.
- **Caddy JSON config requires explicit `subjects` in TLS automation policies.** A bare `:443 { tls internal }` Caddyfile block works (Caddy infers from the host directive), but the equivalent JSON config without `subjects` causes TLS handshake to fail (ERR_SSL_PROTOCOL_ERROR) because Caddy doesn't know which SNI to issue for. Same applies to host-match on HTTPS routes — without it any non-matching SNI returns 404.
- **Bitwarden / password managers and React controlled inputs.** Bitwarden injects values via direct DOM property assignment, which doesn't trigger React's `onChange` event. The injected value is silently overwritten on next render. Workaround: use `useRef` + `defaultValue` for password and username inputs (uncontrolled), read at submit time. Required `name` attributes for the manager to identify fields, plus `autoComplete="current-password"` (or `"new-password"` only when truly setting up).
- **Caddy admin API on `:2019` is open inside the Docker network.** Never publish it externally. The dashboard (`lib/caddy-admin.ts`) reaches it via `http://caddy:2019/load` for dynamic config push. It has no auth, so anything on the compose network can `/load`, and since `--resume` a loaded config also survives restarts.
- **Caddy's `local_certs` global directive doesn't help when `subjects` is missing.** It just sets the issuer policy default; subjects still required.
- **OIDC `redirect_uri` mismatch is silent.** If start sends one URL and the provider's allowlist contains another, the user gets a generic auth error from the provider — no log on our side until you bisect. Always verify with `docker logs hypertrade-dashboard-1 | grep oidc` and the provider's audit log.
- **`PUBLIC_URL` must include the scheme.** `$DEPLOY_HOST` won't work — must be `https://$DEPLOY_HOST` (or `http://...` for dev). Used as base for redirect URLs throughout.
- **Once something has been pushed, Caddy boots from the autosave, not the Caddyfile.** It runs with `--resume` (`docker-compose.yml`), so the last config pushed through `POST /api/tls/configure` survives restarts and recreates. It is stored in `/config/pushed/caddy/autosave.json` on the `caddy_config` volume. The Caddyfile sets `persist_config off`, so it is never saved, and a host that never pushed boots the current Caddyfile every time. After a push, changes to the builders in `lib/caddy-admin.ts` apply only on the next re-POST (`{}` is enough). Caddyfile and `CADDY_HOST` edits never apply that way: a re-POST builds from `caddy-admin.ts`, and the dashboard doesn't get `CADDY_HOST`. Deleting the autosave (command below) is the only way to bring the Caddyfile back. So a proxy fix must land in both files, as #168's did. If Caddy crash-loops with `loading initial config: …`, delete the autosave: `docker run --rm --volumes-from hypertrade-caddy --entrypoint rm "$(docker inspect -f '{{.Image}}' hypertrade-caddy)" /config/pushed/caddy/autosave.json`. That can happen when the image loses a module the saved config uses. The command uses the container's own image, and the restart policy brings Caddy back up on the self-signed bootstrap config.
- **Caddy `http_only_config` is gone.** The bot's old `build_http_only_config()` is now an alias for `build_internal_https_config()` — there is no plain-HTTP fallback any more by design. If TLS breaks, fix it; don't disable TLS.

---

## 10. Glossary

- **Mode:** `paper` / `testnet` / `mainnet` (`EXCHANGE_MODE`); one bot container per tenant and mode.
- **Signal:** what `on_candle()` returns: `action` (OPEN_LONG / OPEN_SHORT / CLOSE_LONG / CLOSE_SHORT), symbol, strategy_name, reason.
- **Tick:** one engine pass (`POLL_INTERVAL_SECONDS`, default 60): fetch candles, ask each enabled strategy, execute signals.
- **Position / Trade:** a `positions` row (one strategy's exposure; the exchange nets per coin, § 9) / a `trades` row (one fill).
- **Equity snapshot:** account value, written every tick unless the balance read fails.
- **Reconcile:** `Repository.reconcile_positions()` compares DB and exchange and closes orphans on both sides; a failed exchange read writes and orders nothing.
- **Allow_multi_coin:** Redis flag. False (default): one strategy per coin, at most one per `family` across coins (`cdc_macd`/`macd_zero`). Checked in `runner._open_refusal()`; families via `registry.get_strategy_family()`.
- **Flip-detect:** OPEN_X while the strategy's DB row is the opposite side → the open is checked first, then a synthesized CLOSE precedes it.
- **Export_state / restore_from_json:** SL/TP/trail/entry saved to `positions.state_json` and restored verbatim on restart (17 of 22 strategies).
- **PUBLIC_URL / CADDY_HOST:** the dashboard's user-facing URL with scheme, the base of every redirect; the host Caddy's bootstrap cert is issued for.
- **vvv_hedge:** in-house, mainnet-only VVV hedge: mandatory EMA-bearish filter plus 2 of 3 other indicators, fixed `holding_vvv` size, hard 10% SL.
- **Backtest CLI:** `cd bot && uv run python -m hypertrade.backtest --strategy <name> --days N` (`--all`; `--no-save` skips `backtest_runs`).

---

This file is the source of truth. When in doubt, update it rather than build
folklore. If a workflow described here is wrong, fix the workflow first and
the code second.
