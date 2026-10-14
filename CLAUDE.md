# HyperTrade — Agent Development Framework

The manual for any Claude agent here (Cline and Codex start from
[AGENTS.md](AGENTS.md), which points here). Mission (§ 1): a
**production-grade autonomous crypto trader** that never silently diverges
from exchange reality. The agent works alone up to a reviewed PR and asks
first when an action is destructive or genuinely ambiguous (§ 7); the
operator merges and deploys.

Roadmap: `docs/plans/next-level-roadmap.md`. Runbooks: § 3. Fixed backlog:
`docs/CHANGELOG.md`.

---

## 0. Personal info & secrets policy — READ EVERY COMMIT

**This repository is public.** Every commit is on GitHub for anyone to read
forever; deleting the file later leaves the value in history. **Never
commit any of these:**

| Type | Example | Where it goes instead |
|---|---|---|
| Telegram bot token | `8639592584:AAGj…` (digits, colon, 35 base64 chars) | Phase (§ 3) |
| HyperLiquid private key | `0x` + 64 hex chars | Phase |
| Cloudflare API token | 40-char base64 | Redis (`dashboard:tls:cf_token`) via operator-only `POST /api/tls/configure`. `lib/tls-config.ts` reads `TLS_CF_API_TOKEN` first, but `docker-compose.yml` doesn't pass it to the dashboard today |
| OIDC client secret | provider-specific | Phase (`OIDC_CLIENT_SECRET`), copied into Redis (`dashboard:auth:oidc:client_secret`) at start |
| `API_KEY` for the bot HTTP API | random string | Phase |
| Phase service token | base64 | only on the host's `~/.phase/` config; NEVER in repo |
| Personal email used live | `you@yourdomain.com` | Phase (`TELEGRAM_*`, OIDC config); generic placeholder in docs (`you@example.com`) |
| Telegram chat ID | 8-12 digit number used as your identity | Phase |
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
3. Optionally scrub older commits with `git filter-repo --replace-text`. Destructive: coordinate first. Anyone who already cloned keeps the old value.
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

**Mission:** build and maintain a HyperLiquid autotrader that
1. Runs the 22 registered strategies byte-faithful to their TradingView ports, where one exists (`vvv_hedge` and `ath_breakout` are in-house designs).
2. Survives network outages, exchange errors, DB hiccups and restarts without losing position state.
3. Keeps trade records, equity snapshots and PnL accurate: the DB always matches the exchange.
4. Surfaces problems via Telegram and the dashboard before they become silent losses.

**The agent cannot guarantee a strategy is profitable.** It *must* keep the
logic identical to the Pine source, propose removing demonstrably broken
strategies (SL=0, sign flips, unit confusion), and recommend disabling, with
evidence, those whose live behavior diverges from their backtest archetype.

**Definition of done: PR → review → merge by the operator.**
1. A feature branch from `origin/master`, committed (§ 7), pushed, with a PR against `master`.
2. Green gates for what it touches, locally and in CI (§ 7). Bot: `cd bot && uv run pytest -q`. Dashboard: `cd dashboard && npm ci && npx tsc --noEmit && npm run lint && npm test && npm run build`.
3. A fixed bug has a test or runtime check that would have caught it.
4. A review pass happened; every finding is fixed or answered (§ 7).
5. The operator merges; an agent only when the operator asks, for that PR.
6. The operator deploys (runbook, § 3): dashboard and Caddy after merge, bot code in the weekly window (roadmap § 4.1), emergency fixes outside it. Then live verification (health-check runbook), by an agent only when asked.

---

## 2. Repo layout

```
bot/hypertrade/
  main.py  # entry; runs every registered strategy. Testnet/mainnet refuse to boot without a DB
  config.py  # pydantic-settings; unknown env vars ignored
  api.py  # aiohttp API: control, positions, indicator-status, hodl, vaults, HL diagnostic
  engine/  # runner.py (tick loop, reconcile 5 min, funding poll 30 min, flip-detect, open gates),
    # control.py (Redis flags), portfolio.py (kill switch, daily PnL), indicators_status.py
  exchange/  # base.py, paper.py (simulated), hyperliquid.py (SDK; reads retry, writes never)
  strategies/  # 22 registered (registry.py), meta/<name>.json; golden_cross.py deliberately unregistered
  hodl/, vaults/  # advisory spot signals, vault scanner: read-only, no orders
  reconcile/fills.py  # prices reconcile closes from HL fills
  data/, events/  # candle feed with retry; Redis pub/sub bus
  notify/telegram.py  # notifier + command bot
  reports/, backtest/  # weekly_eval.py (/eval); backtest CLI + metrics
  db/  # models.py, repo.py (all SQL, reconcile_positions)
bot/tests/, bot/alembic/versions/ (0001 → 0017)
dashboard/src/  # Next.js 16: app/ (pages + /api), proxy.ts (auth gate), components/, lib/
  lib/bot-orchestrator.ts, docker.ts  # spawn bot containers via the host Docker socket
  lib/bot-api.ts, bot-api-key.ts  # proxy to a bot with its per-bot X-Api-Key
  lib/auth-config.ts, oidc.ts, safe-next.ts, phase-sync.ts, caddy-admin.ts, heartbeat-watchdog.ts
caddy/, tv-source/<name>.pine, scripts/, docs/
docker-compose.yml  # postgres, redis, dashboard, caddy, cloudflared (profile public), bot-image (profile build)
```

**No bot runs from `docker-compose.yml`**; `bot-image` only builds
`xupertrade-bot:latest`. The orchestrator turns a `tenant_bots` row and the
tenant's decrypted secrets into one container per tenant and mode
(`xupertrade-bot-<16-hex id>-<mode>`, no published ports,
`restart: unless-stopped`). Telegram, HODL, vaults and key reminders run on
the operator's `HYPERTRADE_SERVICES_OWNER_MODE` bot (paper), other tenants' mainnet bot.

---

## 3. Operating environment

- **Local:** `~/xupertrade/`. **Server:** `root@$DEPLOY_HOST`, code in `/opt/hypertrade/`, key `~/.ssh/hypertrade`.
- **Git:** `https://github.com/kanylbullen/xupertrade.git`, branch `master`; ruleset `default-protection` requires a PR, blocks force-push and deletion, no bypass actors.
- **Postgres** `127.0.0.1:5432`, **Redis** `127.0.0.1:6379`: loopback only. Redis has no `requirepass`, so reachability *is* its access control; it holds the session-signing and OIDC secrets, the CF token and the per-bot API keys. **Never publish it beyond loopback without `requirepass`** threaded through dashboard and bots.
- **Bot APIs:** paper `:8000`, testnet `:8001`, mainnet `:8002`, in the containers only; most routes need the bot's `X-Api-Key`.
- **Dashboard:** `127.0.0.1:3000` for probes; users come via Caddy (`:443`, LAN) or Cloudflare tunnel. **Caddy:** `:80` → HTTPS, `:443`, `:443/udp`; admin API `:2019` internal only. TLS: Let's Encrypt via Cloudflare DNS-01, else self-signed for `CADDY_HOST`.
- **Auth:** basic or OIDC via Phase (`AUTH_MODE`, `OIDC_*`, copied to Redis at boot by `lib/phase-sync.ts`) or `scripts/set-basic-auth.sh`, no settings UI. Stored mode gone and no config left → `locked`; fresh install (no tenant) → `disabled`, nobody can sign in.
- **Secrets:** in Phase, injected by `phase run --`; no `.env` on the host.

**Runbooks** in `docs/runbooks/<name>.md`, the operator's (an agent runs one only when asked):
- `deploy`: dashboard, Caddy and weekly bot deploys, `GIT_SHA` checks, cache trap, Redis hazard, prune cron
- `merge-gates`: required CI checks, emergency bypass (not set up)
- `health-check`: DB ↔ exchange parity, a bot's API, logs when `docker logs` breaks
- `dashboard-auth-recovery`: "Authentication is locked" on `/login`; fresh-install sign-in
- `postgres-password-rotation`: rotating `POSTGRES_PASSWORD`
- `phase-secrets`: adding a secret, Phase on a new host

---

## 4. Subagents

Model choice and delegation follow `~/CLAUDE.md` (outside git), *"Offloada
arbete till subagenter på Opus och Sonnet"*; no model table here. Per it,
production data or secrets (live DB, Redis, backfills) stay in the main
session. Also (roadmap § 4.1): order-path code and migrations are Opus or
main-session work, never sonnet or Kanban.

---

## 5. Backlog

Open items only. The PR that fixes one moves its entry verbatim to
`docs/CHANGELOG.md`, ticked and citing the PR number.
Never delete an entry. Add one for a defect you find and don't fix now.
Planned initiatives live in the roadmap.

### Open — Critical (blocks safe operation)

(none currently)

### Open — High (impacts trading correctness)

- [ ] **A refused edge exit is lost once its bar ends.** Master drops any refused exit; NU-5a re-sends one while it fires, so a crossunder, Supertrend flip or daily_long_0830's 08:00 bar is lost, unseen by reconcile, as is any exit while its strategy cannot run or restarts. Short fills never escalate. Owner: NU-5 C (engine-owned close), after its plan. #192: exits-never-blocked-1/5; `b292005`.

### Open — Medium

- [ ] **Volatility-adjusted sizing (option C from Kelly discussion).** ATR-normalized sizing on a new `RISK_BUDGET_USD`, `notional = RISK_BUDGET_USD / (atr × atr_mult)`, replaces a fixed `MAX_POSITION_SIZE_USD` (kept as a hard cap), so every trade risks about the same dollars whatever the volatility. Industry standard, no statistical estimation; ~3-4h, defensive. *(Roadmap SE-2.)*
- [ ] **Drawdown-based auto-scaling (option B from Kelly discussion).** `MAX_STRATEGY_DRAWDOWN_PCT` per strategy: at 80% of the cap, halve effective margin until 7-day rolling PnL > 0. Limits exposure on degrading strategies without Kelly's stationary-distribution assumption. *(Roadmap SE-2.)*
- [ ] **Rows larger than the exchange are never settled from fills.** A close clamped after a partial liquidation closes the whole row without booking the rest (NU-5a's "mindre än raden"); flat-all books each row on a coin in full under one oid, so the second fails. Owner: NU-5 settlement PR. #192: failure-injection-2, double-close-2, booking-2, flip-and-flat-1, r1-booking-3/5, r1-flip-and-flat-1, r1-tests-and-noise-1; `3ea78b7`, `b292005`, `3039fe5`, `8d27bbe`.
- [ ] **A late fill is booked under a fresh uuid at a guessed price:** the position's entry or the IOC limit (after NU-5a: the mid), never its own fill. Owner: NU-5 late-fill PR. #192: booking-1, double-close-1, booking-5, tests-real-2, r1-double-close-3, r1-booking-4; `3039fe5`, `8d27bbe`.

### Open — Low

- [ ] **A pass-2 orphan close that fills short releases the NU-2 hold** while part of the orphan is open. ~6 lines: an NU-2 follow-up, or NU-5a if its reviewer asks. #192: booking-3; `3039fe5`.
- [ ] **The "flip open failed" Telegram lacks `order.error`** (`_flat_after_flip_open_failed`); one line in a later order-path PR. #192: noise-and-logs-3; `b292005`.
- [ ] **`get_current_price` logs an ERROR traceback per failed allMids read**, plus one per tick for each close NU-5a retries. Pre-existing; accepted noise. #192: lifecycle-2; `47f844e`.

---

## 6. Working principles

**Investigation before code.** Reproduce or observe the problem (log line,
DB row, screenshot), read the relevant code top to bottom, then write the
fix and a check that would have caught it. A fix "obvious" without the
first step is probably wrong.

**Keep DB and exchange in lockstep:** divergence is the most dangerous
failure here, and we have lived through it. Code that opens or closes
positions writes to the DB **before** the order (or records the order ID
and reconciles), tolerates an order that succeeds while the DB write fails,
and is idempotent on retry. Reconcile is the safety net, not the strategy.

**No features without a need.** Resist more strategies, pages and
abstractions; the backlog and the roadmap are the plan.

**Logs are the API.** Every open, close, skip, reconcile and error logs
strategy, symbol, side, size, price and the *reason*.

**Telegram is for humans:** `trade.executed`, `position.closed`, `error`,
vault and HODL state changes; never `signal.generated` or tick noise.

**Money:** close sizes come from `position.size` in the DB, never
recomputed; never compare float prices with `==` (use `abs(a - b) < 1e-6`);
fees are subtracted at trade-record time; `notional = margin * leverage`,
and stop distances are on price.

**Testing.** Every registered strategy has a test class in
`bot/tests/test_strategies/` (`vvv_hedge` in `bot/tests/test_vvv_hedge.py`):
warmup guard, entry signal, restore without instant close, SL exit; a new
strategy adds one. `test_universal_invariants.py` property-tests the
registry. `uv run pytest -q` in `bot/`: 1201 passed, 1 skipped, 3 xfailed
on 2026-09-25. Paper mode is the integration test.

---

## 7. Workflow per task

1. State the task in one sentence; check § 5 and the roadmap.
2. Investigate: code, logs, DB rows (read-only).
3. Non-trivial work: a plan in `docs/plans/<feature>.md`, signed off by the operator before code.
4. `git fetch origin && git switch -c <type>/<short-name> origin/master`; types `feat`, `fix`, `docs`, `refactor`, `chore`; kebab-case, specific (`feat/vault-scanner`).
5. Keep AGENTS.md's PR rules: ≤600 added lines, mechanical refactors apart, one open order-path PR at a time.
6. Gates (§ 1), commit, `git push -u origin <branch>`, `gh pr create --base master`; PR body `## Summary` (what, why), `## Test plan` (gates, checks), `## Notes for reviewer` (subtleties, follow-ups).
7. Review (below); fix or answer every finding.
8. The operator merges (`gh pr merge --squash --delete-branch`) and deploys (§ 1).

Commit messages: `<type>: <imperative summary, <72 chars>`, a WHY
paragraph, and `Co-Authored-By: Claude <model> <noreply@anthropic.com>`.

**No direct pushes to `master`.** The ruleset refuses them; the PreToolUse
hook `.claude/hooks/guard_bash.py` blocks them locally, plus `--no-verify`,
`commit -n` and `core.hooksPath` swaps (gaps: its docstring). An emergency
fix is a PR the operator merges without waiting for review; a broken master
commit is undone by a `git revert` PR, never a force-push. Only the operator
lifts the hook, by starting Claude Code with `XUPERTRADE_ALLOW_MASTER_PUSH=1`
(the ruleset still refuses) or `XUPERTRADE_ALLOW_NO_VERIFY=1`, so an agent
rephrases a verified false positive (§ 0) instead of skipping. Tests:
`python3 .claude/hooks/test_guard_bash.py`.

### Review before merge: no automated code reviewer

`master` requires the CI checks `bot`, `dashboard`, `migrations` and
`gitleaks`, emergency fixes included (no bypass yet, § 3); wait with
`gh pr checks <n> --watch`. None reviews code. Reading Copilot's silence
(it stopped after #134) as approval once merged five unreviewed PRs
(#156–#160).

**A PR with no review is unreviewed, not approved.** Before merge, one of:
1. A human reviewed the diff; every comment is fixed or answered.
2. An agent review ran; every finding was classified (bug / nit / spurious), fixed or answered, and noted in the PR. `/review` uses 3–4 angles on a small diff and all eight on the order path, money or migrations; `.github/review-team/` has 11 repo-native lenses.
3. The operator waived review for a trivial change.

No review → leave the PR open: green gates prove the tests pass, not that
the code is right. Reply inline:
`gh api -X POST repos/<owner>/<repo>/pulls/<N>/comments/<id>/replies -f body="..."`.

### When to ask the user vs. just do it

**Just do it (in a PR):** code fixes, refactors, tests; logging, retry and
reconcile improvements; docs, `.env.example`, README; features that fit the
existing architecture; recommending a strategy disable, with evidence.

**Ask first (plan in `docs/plans/`):** merging or deploying; the production
host, DB rows or Redis; disabling a strategy in live config; real funds or
mainnet trading; `git reset --hard` on shared branches or force-push; an
architecture change touching >5 files; removing (not deprecating)
strategies, columns or endpoints; features that don't fit the architecture
(e.g. the vault scanner). In doubt, write the plan first.

---

## 8. Strategy evaluation policy

Weekly (Sunday) or on demand, per strategy over its last 7 days of
`mode='testnet'` trades: trades, win rate, realized PnL, average PnL per
trade, max consecutive losses; compared with its archetype (mean reversion
~50%+ win rate, momentum <40% with bigger wins); flag 0 trades for 14+
days, realized PnL over 2σ below the backtest expectation, and
gross-positive but net-negative after fees and funding.
`hypertrade.reports.weekly_eval` computes it and posts to Telegram (Sunday
18:00, or `/eval [days]`) and stdout; no file. Durable write-ups go by hand
under `bot/reports/`.

Testnet is a pipe test for execution, not evidence of edge (roadmap
decision 5.6). The agent **recommends** disable, the user **decides**, via
the dashboard `/options` page or `POST /api/control/strategy/{name}/toggle`,
never by editing strategy code.

---

## 9. Common pitfalls — read before debugging

- **`asyncio.to_thread` + `asyncio.run` deadlock with asyncpg in Python 3.13.** It burned us with Alembic. Never call `asyncio.run` in a thread already in an event loop; use `loop.run_in_executor`, or run blocking code in a real subprocess.
- **HyperLiquid position is netted per-coin.** Two strategies on opposing sides of one coin show as the net, and the DB diverges unless `allow_multi_coin=False` is enforced (the default now).
- **HyperLiquid sizes have per-asset `szDecimals`.** BTC is 5 decimals, SOL is 2. Round before submitting or get cryptic 422s. The exchange wrapper does this; don't bypass it.
- **HyperLiquid prices have a 5-significant-figures rule.** Same wrapper handles it.
- **Telegram HTML parse mode breaks on raw `<` and `>`** in dynamic strings. Always `html.escape()` user- or model-supplied text; `notify/telegram.py` already does.
- **`docker compose exec -T`** is required for non-TTY commands over SSH; without `-T` the command hangs.
- **Postgres `is_open == True` in SQLAlchemy** must be `== True` (not `is True` or bare `is_open`). Drizzle and SQLAlchemy lift `True` to `1` in the SQL only with an explicit comparison.
- **Settings are loaded once at process start.** A `.env` change needs a container restart; runtime overrides go through Redis (`BotControl`), not env vars.
- **`req.url` inside Docker returns the container's bound hostname**, not the public one users see through the reverse proxy, so a redirect built from it (NextResponse.redirect, OIDC redirect_uri, login redirects) sends users to `https://<docker-id>:3000/`. Always resolve through `PUBLIC_URL` (fallback `DASHBOARD_URL`). Pattern: `lib/oidc.ts:resolveRedirectUri()`, `proxy.ts` login fallback, `app/api/auth/oidc/callback/route.ts:publicLoginUrl()`.
- **Caddy JSON config requires explicit `subjects` in TLS automation policies.** A bare `:443 { tls internal }` Caddyfile block works (Caddy infers them from the host directive); the same JSON without `subjects` fails the TLS handshake (ERR_SSL_PROTOCOL_ERROR), as Caddy doesn't know which SNI to issue for. HTTPS routes need a host match too, or any non-matching SNI returns 404.
- **Bitwarden / password managers and React controlled inputs.** Bitwarden sets values by DOM property assignment, which fires no `onChange`, so React's next render silently overwrites them. Use uncontrolled username and password inputs (`useRef` + `defaultValue`) read at submit, `name` attributes so the manager finds them, and `autoComplete="current-password"` (`"new-password"` only when truly setting up).
- **Caddy admin API on `:2019` is open inside the Docker network.** Never publish it. The dashboard (`lib/caddy-admin.ts`) pushes config via `http://caddy:2019/load`. It has no auth: anything on the compose network can `/load`, and with `--resume` a loaded config survives restarts.
- **Caddy's `local_certs` global directive doesn't help when `subjects` is missing.** It only sets the issuer policy default.
- **OIDC `redirect_uri` mismatch is silent.** If start sends a URL the provider's allowlist lacks, the user gets a generic provider error and we log nothing until you bisect. Verify with `docker logs hypertrade-dashboard-1 | grep oidc` and the provider's audit log.
- **`PUBLIC_URL` must include the scheme.** `https://$DEPLOY_HOST` (`http://...` for dev), not `$DEPLOY_HOST`; it is the base of every redirect URL.
- **Once something has been pushed, Caddy boots from the autosave, not the Caddyfile.** With `--resume` (`docker-compose.yml`) the last config pushed via `POST /api/tls/configure` survives restarts and recreates, in `/config/pushed/caddy/autosave.json` on the `caddy_config` volume. The Caddyfile sets `persist_config off`, so it is never saved; a host that never pushed boots the current Caddyfile every time. After a push, builder changes in `lib/caddy-admin.ts` apply on the next re-POST (`{}` is enough), but Caddyfile and `CADDY_HOST` edits never do: a re-POST builds from `caddy-admin.ts`, and the dashboard doesn't get `CADDY_HOST`. Only deleting the autosave (below) brings the Caddyfile back, so a proxy fix must land in both files, as #168's did. If Caddy crash-loops with `loading initial config: …` (e.g. the image lost a module the saved config uses), delete the autosave: `docker run --rm --volumes-from hypertrade-caddy --entrypoint rm "$(docker inspect -f '{{.Image}}' hypertrade-caddy)" /config/pushed/caddy/autosave.json`. It uses the container's own image; the restart policy brings Caddy back on the self-signed bootstrap config.
- **Caddy `http_only_config` is gone.** The bot's old `build_http_only_config()` is now an alias for `build_internal_https_config()`: by design there is no plain-HTTP fallback. If TLS breaks, fix it; don't disable TLS.

---

## 10. Glossary

- **Mode:** `paper` / `testnet` / `mainnet` (`EXCHANGE_MODE`); one container per tenant and mode (§ 2).
- **Signal:** what `on_candle()` returns: `action` (OPEN_LONG/OPEN_SHORT/CLOSE_LONG/CLOSE_SHORT), symbol, strategy_name, reason.
- **Tick:** one engine pass (`POLL_INTERVAL_SECONDS`, default 60): fetch candles, ask each enabled strategy, execute signals.
- **Position / Trade:** a `positions` row (one strategy's exposure; the exchange nets per coin, § 9) / a `trades` row (one fill).
- **Equity snapshot:** account value, written each tick unless the balance read fails.
- **Reconcile:** `Repository.reconcile_positions()` compares DB and exchange, closes orphans on both sides; a failed exchange read writes and orders nothing.
- **Allow_multi_coin:** Redis flag. False (default): one strategy per coin, at most one per `family` across coins (`cdc_macd`/`macd_zero`). Checked in `runner._open_refusal()`; families via `registry.get_strategy_family()`.
- **Flip-detect:** OPEN_X while the strategy's DB row is the other side → the open is checked, then a synthesized CLOSE goes first.
- **Export_state / restore_from_json:** SL/TP/trail/entry saved to `positions.state_json`, restored verbatim on restart (17 of 22 strategies).
- **PUBLIC_URL / CADDY_HOST:** the dashboard's user-facing URL with scheme, base of every redirect; the host of Caddy's bootstrap cert.
- **vvv_hedge:** in-house, mainnet-only VVV hedge: mandatory EMA-bearish filter plus 2 of 3 other indicators, fixed `holding_vvv` size, hard 10% SL.
- **Backtest CLI:** `cd bot && uv run python -m hypertrade.backtest --strategy <name>`/`--all` needs `--tenant-id`/`TENANT_ID` or `--no-save`.

---

This file is the source of truth. In doubt, update it rather than build
folklore. If a workflow here is wrong, fix the workflow first and the code
second.
