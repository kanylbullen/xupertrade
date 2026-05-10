# Pre-mainnet Security Audit — 2026-05-10

**Scope:** real-money risk surface for first mainnet trade. Trace
order/close/kill paths, mode branches, recent PRs #15–26.

**Bottom line:** **NO-GO** until at least the four CRITICAL items
are fixed. Two of them (broken total-exposure cap, in-process
daily-loss kill-switch) make the documented risk caps effectively
non-binding.

---

## Status snapshot

- Previous audit: 2026-04-25 (26 findings, all C/H addressed in PRs #10–#23)
- This audit: post PRs #15–#26 (12 PRs over 2026-05-09 → 05-10)
- New attack surface considered: HL timeouts, atomic transactions,
  trade-rate alarm, parity check, vault gating, transient-error
  filter, Telegram setMyCommands, security policy, pr-watch script

---

## CRITICAL (NO-GO blockers)

### C1 — `MAX_TOTAL_EXPOSURE_USD` cap uses position COUNT × cap, not actual margin

- **File:** `bot/hypertrade/engine/runner.py:715-740`
- **Description:** Computes `current_margin` correctly (line 722-725,
  sum of `size * entry / leverage`) then **discards it** and uses
  `len(open_pos) * settings.max_position_size_usd` for the actual
  decision. Means:
  - Strategies that emit `Signal(size=X)` (vvv_hedge with 400 VVV at
    $5+ = real $2k+ notional) count as "1 × $200" toward exposure.
  - 5x leverage strategies count as `MAX_POSITION_SIZE_USD` even
    though their actual margin is `MAX_POSITION_SIZE_USD / 5`.
  - The check effectively just caps NUMBER of open positions at
    `MAX_TOTAL_EXPOSURE_USD / MAX_POSITION_SIZE_USD = 25`.
- **Fix:** replace line 728-729 with
  `if current_margin + new_position_margin > settings.max_total_exposure_usd:`
  where `new_position_margin = (signal.size * current_price if signal.size else settings.max_position_size_usd) / max(leverage, 1)`.

### C2 — Daily-loss kill-switch is in-memory only; resets to 0 on every restart

- **File:** `bot/hypertrade/engine/portfolio.py:15,33,44`
- **Description:** `PortfolioManager._daily_pnl` is a Python int
  initialized to 0.0. Not persisted. After a $400 loss, a
  `docker compose restart bot-mainnet` (e.g. for any reason —
  health-check restart, deploy, OOM kill) zeroes the counter and
  trading resumes despite blowing through `MAX_DAILY_LOSS_USD=100`.
  Container has `restart: unless-stopped` (compose:8) → auto-restart
  on crash already in play.
- **Fix:** persist daily PnL to Redis on `record_pnl`, load on
  `PortfolioManager.__init__`. Key:
  `hypertrade:{mode}:daily_pnl:{YYYY-MM-DD}` with TTL 48h.

### C3 — All 21 strategies auto-instantiate on mainnet bot startup; `disabled` set is empty by default

- **File:** `bot/hypertrade/main.py:109-110` + `bot/hypertrade/strategies/registry.py:28-56`
- **Description:** `strategies = [get_strategy(name) for name in list_strategies()]`
  runs every registered strategy. `get_disabled_strategies()` returns
  the Redis set, which on a brand-new mainnet deploy is empty. Result:
  **every strategy starts trading on mainnet immediately on first
  start**, including ones we know are net-negative in 180d backtest
  (penguin_volatility -10.3%, ema_crossover before fix -12.4%, etc.).
- **Fix:** BEFORE first mainnet start: SSH in,
  `redis-cli SADD hypertrade:mainnet:control:disabled <every-strategy-name-except-the-one-you-want>`.
  Or: add a `MAINNET_ENABLE_LIST` env var that whitelists explicitly.

### C4 — Telegram has zero control over the mainnet bot

- **File:** `docker-compose.yml:103` (`TELEGRAM_ENABLED: "false"` for
  bot-mainnet) + `bot/hypertrade/main.py:116-118`
- **Description:** TelegramNotifier is constructed with the LOCAL bot's
  `BotControl` (testnet's), not the mainnet bot's. The Redis keys are
  mode-namespaced (`hypertrade:testnet:control:flat_request_id`). When
  the operator sends `/flat confirm` or `/pause` to Telegram, those
  write to **testnet's** keys. Mainnet bot polls
  `hypertrade:mainnet:control:*` and never sees them.
- **Fix:** wire TelegramNotifier with a `dict[mode, BotControl]` and
  add `/flat-mainnet`, `/pause-mainnet` etc.; or make `/flat`, `/pause`
  accept a `mainnet|testnet` arg and write to the corresponding keys.

---

## HIGH (strongly recommended before mainnet)

### H1 — Strategy `leverage` attribute scales NOTIONAL but is not re-pushed to HL on every order

- **File:** `bot/hypertrade/engine/runner.py:1254-1263` + `bot/hypertrade/main.py:166-184`
- **Description:** `_calculate_size` returns `notional = MAX_POSITION_SIZE_USD * leverage`
  and divides by price for base units. But HL leverage is set ONCE at
  startup (`update_leverage`) and only re-pushed when the dashboard
  `/api/control/strategy/{name}/leverage` endpoint is hit. If the
  operator manually edits Redis (`HSET hypertrade:mainnet:control:leverage strat_name 10`),
  `s.leverage` updates next tick but HL still has the startup leverage.
  Order computes 10× notional, HL accepts it at default leverage,
  **margin used = 10× expected.** Liquidation path.
- **Fix:** at the start of each `_execute_signal` for OPEN actions,
  re-compute per-coin max leverage and call
  `exchange.update_leverage(symbol, lev)` if it differs from a tracked
  last-pushed value.

### H2 — HL order timeout (15s) marks orders REJECTED that the exchange may still fill

- **File:** `bot/hypertrade/exchange/hyperliquid.py:296-321`
- **Description:** `place_order` wraps the SDK call in
  `asyncio.wait_for(timeout=15s)`. On timeout, returns
  `OrderStatus.REJECTED` and the runner skips the DB write. If HL
  eventually fills the order, **exchange has a position, DB has nothing.**
  Reconcile catches it 5 min later and force-closes — but during those
  5 min, an SL-driven strategy can't manage the position, and the close
  is a market order at whatever price HL has THEN.
- **Fix:** when `place_order` times out, do not return REJECTED.
  Instead poll `exchange.get_position(symbol)` for ~30s; if a new
  position appears, treat it as filled at the observed entry price.

### H3 — Kill-switch (`KILL_SWITCH=true`) blocks SL/TP exits as well as new opens

- **File:** `bot/hypertrade/engine/portfolio.py:28-30` + `bot/hypertrade/engine/runner.py:620-622`
- **Description:** `check_risk_limits()` returns False when
  `kill_switch` is on, applied uniformly to both OPEN and CLOSE
  signals. Flipping the kill switch on a position-already-open mainnet
  bot **freezes the position** — strategy SL/TP exits don't fire.
  Operator must use flat-all to unwind (which does bypass kill_switch).
- **Fix:** in `_execute_signal`, only enforce kill_switch on OPEN_LONG/
  OPEN_SHORT, never on CLOSE_*.

### H4 — `_check_parity_after_trade` per-coin tolerance is loose for low-szDecimals coins

- **File:** `bot/hypertrade/engine/runner.py:918-992`
- **Description:** Tolerance = `10 * 10**(-szDecimals)`. For SOL
  (szDecimals=2) that's 0.1 SOL ≈ $15 at $150. For VVV
  (szDecimals likely 0-1) tolerance could be 1+ unit ≈ $5+. Means a
  partial-fill drift up to that magnitude won't trip the alert.
- **Fix:** tolerance should be
  `min(10 * 10**(-szDecimals), 0.5% * size)` so the limit is
  meaningfully tight on small positions.

### H5 — `_flat_all_positions` writes Trade and PositionRecord in TWO separate sessions

- **File:** `bot/hypertrade/engine/runner.py:495-515`
- **Description:** `repo.record_trade(...)` then `repo.close_position(...)`
  — the M8 bug (fixed everywhere except here). SIGTERM/crash between
  them leaves Trade row recorded with no `is_open=false` update; on
  next startup reconcile sees DB-open + exchange-flat → orphan-closes
  with PnL=0, double-counting.
- **Fix:** use `record_trade_and_close_position` (already exists, same
  atomicity).

### H6 — `_flat_all_positions` uses EXCHANGE entry_price for realized PnL, not the strategy's DB entry

- **File:** `bot/hypertrade/engine/runner.py:488-493`
- **Description:** HL exchange-side `entry_price` is the
  volume-weighted average across all add-to-position legs. If two
  strategies opened on same coin and one was DB-orphaned earlier,
  exchange entry won't match either strategy's view. Realized PnL
  recorded to DB will be wrong (could be off by hundreds of dollars
  on partial-leg positions).
- **Fix:** when flat-all closes a coin, look up the open DB position's
  `entry_price` and use that for the recorded PnL.

### H7 — Kill switch is env-only, requires container restart to flip

- **File:** `bot/hypertrade/config.py:70` + `bot/hypertrade/engine/portfolio.py:28-30`
- **Description:** `settings.kill_switch` is read at process start.
  Cannot be flipped at runtime. Operator wanting to "kill all trading
  right now" must `docker compose restart` after setting
  `KILL_SWITCH=true` in `.env` — meanwhile the running tick can still
  place orders.
- **Fix:** read kill-switch from Redis (`hypertrade:control:kill_switch`)
  on every tick, fall back to env. New endpoint
  `POST /api/control/kill-switch` (API_KEY-gated) sets it.

### H8 — `signal.size`-overriding strategies bypass `MAX_POSITION_SIZE_USD` entirely

- **File:** `bot/hypertrade/engine/runner.py:742`
  (`size = signal.size or self._calculate_size(...)`)
- **Description:** vvv_hedge emits `Signal(size=400)` and the engine
  uses 400 verbatim — no clamp. On mainnet with VVV ≈ $5 that's a $2k
  notional from a strategy whose risk is governed by a single 10% hard
  SL. Operator who bumps `holding_vvv` to 4000 by accident gets a
  $20k position with the same SL.
- **Fix:** add a hard ceiling — reject if `size_usd > N * MAX_POSITION_SIZE_USD`
  for some N (e.g. 10), with a log line naming the strategy.

---

## MEDIUM (operational hardening)

### M1 — Reconcile force-closes ANY exchange-side position not in DB

- **File:** `bot/hypertrade/db/repo.py:566-591`
- **Description:** Calls `exchange.place_order(MARKET)` directly. If
  the operator manually opens a hand-managed position on the HL UI on
  mainnet, the bot will market-close it within 5 minutes. No
  notification, no override flag.
- **Fix:** add an env var `RECONCILE_CLOSE_EXCHANGE_ORPHANS` defaulting
  to true on testnet/paper, false on mainnet. When false, log + emit
  ErrorOccurred event to Telegram instead of closing.

### M2 — Reconcile orphan-close uses `exit_price = entry_price` and `pnl = 0`

- **File:** `bot/hypertrade/db/repo.py:512-515`
- **Description:** When DB says open but exchange says flat, the row is
  closed with PnL=0. The actual realized PnL is whatever happened on
  the exchange between the open and now — could be ±$500. By recording
  0, the bot's DB-side daily P&L is silently wrong, which feeds
  `MAX_DAILY_LOSS_USD` (broken) and the daily/weekly Telegram digest.
- **Fix:** at orphan-close time, query HL's user-fills endpoint for
  the close fill and record actual realized PnL.

### M3 — `hash_supertrend` and other "no-SL" strategies have unbounded loss potential

- **Files:** `bot/hypertrade/strategies/hash_supertrend.py:17-18`,
  candidates `daily_long_0830`
- **Description:** Pine source has no SL — only flips on direction
  change. On mainnet with 1h bars and a sharp gap, a position can
  bleed to liquidation before next opposite signal.
- **Fix:** add a "hard backstop SL" config (e.g. -5% of entry) to all
  flip-only strategies.

### M4 — Heartbeat staleness: API endpoint reports it but nothing acts on it

- **File:** `bot/hypertrade/api.py:298-308` + `bot/hypertrade/engine/control.py:152-160`
- **Description:** `beat_heartbeat` writes a Redis key with TTL=300s on
  each tick. `get_heartbeat` returns "stale" if age > 180s.
  **No alerting on staleness, no auto-pause.**
- **Fix:** add an external watchdog (UptimeRobot) hitting the heartbeat
  endpoint, OR have the testnet bot's Telegram poll the mainnet
  heartbeat every 5 min and alert on staleness.

### M5 — `auth_verify_basic` no rate limiting beyond bcrypt latency

- **File:** `bot/hypertrade/api.py:238-268`
- **Description:** Docstring claims rate-limit; only bcrypt latency
  (~250ms) limits attempts. Single-user system makes this acceptable.

### M6 — All bot APIs return CORS `Access-Control-Allow-Origin: *` by default

- **File:** `bot/hypertrade/api.py:21-32`
- **Description:** `_DASHBOARD_ORIGIN = os.getenv("DASHBOARD_URL", "*")`.
  If operator doesn't set `DASHBOARD_URL` on bot containers (compose
  only sets it on dashboard), `*` is sent. Combined with empty API_KEY
  = browser exfil potential.
- **Fix:** refuse to start with `*` when `EXCHANGE_MODE=mainnet`.

### M7 — Cooldown bar-aware fix may not cover all strategies

- **Files:** `bot/hypertrade/strategies/{supertrend,qullamagi_breakout}.py`
- **Description:** Both have a `cooldown_bars` concept. Need to verify
  both increment per actual bar transition, not per tick (the
  hash_momentum spam-class bug from 2026-05-09).

### M8 — PR #22 atomic transactions edge case: `session.begin()` failure

- **File:** `bot/hypertrade/db/repo.py:121-148, 176-207`
- **Description:** If Postgres restarts or has lock contention,
  `session.begin()` raises AFTER the order was sent to HL. HL has the
  position, DB doesn't, retry not queued — only reconcile catches it
  5 min later. Same class as H2.

---

## LOW (polish)

### L1 — `hyperliquid_diagnostic` constructs a fresh `HyperLiquidExchange` per call

- **File:** `bot/hypertrade/api.py:67-90`
- API_KEY-gated, but each call re-runs the init-retry loop.
  Mainly DoS curiosity. Suggested fix: cache the exchange instance.

### L2 — `.env` `HYPERLIQUID_PRIVATE_KEY` and `HYPERLIQUID_MAINNET_PRIVATE_KEY` sibling vars

- **Files:** `docker-compose.yml:81,101` + `.env.example:8,14`
- A copy-paste swap of the two values will be silently accepted.
  Suggested fix: at boot, log `signer_address` in WARNING and require
  match against expected-address env var.

### L3 — PR #15 SL/TP "live bar" fix is functionally a no-op

- **Files:** `bot/hypertrade/strategies/{hash_momentum,keltner_breakout,ema_crossover,pivot_supertrend}.py`
  + `bot/hypertrade/engine/runner.py:564`
- Runner already strips the forming bar at runner.py:564, so
  `df.iloc[-1]` is the LAST CLOSED bar — same as `closed.iloc[-1]`.
  Behavior is correct (matches Pine semantics), but the operator's
  mental model of "we'll exit intra-bar on SL hit" is wrong.
  Suggested fix: docs only — note in CLAUDE.md.

### L4 — `--profile mainnet` gate works, but `HYPERLIQUID_MAINNET_ACCOUNT_ADDRESS` empty default is silently accepted

- **File:** `docker-compose.yml:101-102`
- If operator forgets the address, defaults to "signer wallet IS the
  trading account". For API-wallet-pattern users (signing wallet ≠
  main account), bot will trade from the SIGNING wallet's account.
  Suggested fix: WARNING log when empty in mainnet mode.

---

## Verified clean (notable non-findings)

- HL private key flows env → `Account.from_key()` → SDK; never logged,
  never returned, never in event payloads.
- PR #19 flip-detect-abort works correctly (verified by reading).
- Trade-rate auto-pause persists across restart (Redis-backed).
- PR #25 setMyCommands has no injection surface.
- Vault scanner correctly gated to testnet bot only.

---

## GO / NO-GO for first mainnet trade

**NO-GO until at minimum these four items are addressed:**

1. **Fix `MAX_TOTAL_EXPOSURE_USD` to use real margin sums** (C1).
   The cap is currently a position-count cap, not a dollar cap.
2. **Persist `daily_pnl` to Redis** (C2). The MAX_DAILY_LOSS kill
   resets on every restart.
3. **Disable all 21 strategies in mainnet Redis BEFORE first start**
   (C3). Run `redis-cli SADD hypertrade:mainnet:control:disabled <name>`
   for every strategy except the one you intend to trade.
4. **Add Telegram control over mainnet bot OR commit to dashboard-only
   emergency stop** (C4).

**Strongly recommended (HIGH-band):**

5. Verify `API_KEY` is set in mainnet `.env`.
6. Cap vvv_hedge's `Signal(size=…)` notional (H8).
7. Verify per-coin HL leverage matches each strategy's `s.leverage`
   BEFORE first trade (H1).
8. Move kill-switch to Redis (H7), AND fix kill-switch to allow CLOSE
   signals through (H3).
9. Set `RECONCILE_CLOSE_EXCHANGE_ORPHANS=false` on mainnet (M1).
10. Fix `_flat_all_positions` (H5+H6).

**First-trade rollout suggestion:**

- Set `MAX_POSITION_SIZE_USD=20` and `MAX_DAILY_LOSS_USD=20` for the
  first week (intentionally tiny).
- Enable exactly ONE strategy (e.g. `bb_short` — only +5.5% in 180d
  backtest).
- Run for 7 days and verify per-trade execution matches expectations.
- Only then bump the caps.

The codebase is well-engineered for testnet — atomic writes, parity
checks, reconcile, retries, the trade-rate alarm. The four CRITICAL
items are all "the gates that should bound real-money exposure are
not actually binding." Fix those and the system is ready to roll out
conservatively.

---

## Implementation plan (3 PRs)

| PR | Findings | Scope |
|---|---|---|
| **A** | C1, C2 | Risk-cap correctness — both in PortfolioManager + runner |
| **B** | C3, C4 | Mainnet rollout safety — disabled-default + Telegram-mainnet wiring |
| **C** | H1, H2, H3, H5/H6 | Operational hardening — leverage push, order-timeout poll, kill-switch allow-exits, flat-all atomic |

Mediums/Lows triaged for separate follow-up bundles after first
successful mainnet trade.
