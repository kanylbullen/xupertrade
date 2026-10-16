# NU-5: defects deferred from the #192 split

PR #192 (NU-5a) grew to +5,884 lines over three fix rounds. On
2026-10-14 the operator split it along the roadmap's NU-5, in the order
A0 → a → b → c → settlement → C → late fills → d → e (roadmap, NU-5
"Hur"). A0 is #196, a is NU-5a on `fix/nu-5a-reduce-only-closes`
(called A below), and C waits for its plan (#197). The split leaves
behind defects that #192 had fixed, and A's own review deferred more.
This file is the full record of them, written by D0 (#198), so they
outlive #192's comments.

CLAUDE.md § 5 has one line per record, in its severity section, linking
here. The PR that fixes a record moves that line to `docs/CHANGELOG.md`
(§ 5's rule) and adds its PR number to the record's heading here.
Records are never deleted.

**Ids.** #192's ids are the ones its review triage comments use; an
`r1-` id is from the round fixed in `8d27bbe`. `a-` ids are from A's
first review round, triaged in the message of A's commit `c4cc958`.
**Source** names the #192 commits (branch `fix/reduce-only-closes`) that
hold the code or tests to port, where one exists.

Every deferred finding, or each part of one, has a home: a record's
**Findings** line, or one of the three lists at the end (carried by a
planned PR, accepted with no owner, process items). The lists have no
§ 5 line.

---

## D1 · A refused edge exit is lost after its bar or a restart

- **Severity:** High.
- **Findings:** #192 exits-never-blocked-1, exits-never-blocked-5 (the
  short-fill part); A a-exits-never-blocked-3.
- **What:** master drops a refused plain close at once. A re-arms the
  strategy with its pre-signal state, so the exit is sent again while
  the strategy's own rule still fires. The band widens after 3 refusals,
  and a WARNING is logged when the exit stops firing. Three gaps remain:
  - An edge-triggered exit fires on one bar: a crossunder in cdc_macd or
    macd_zero, the Supertrend flip in hash_supertrend, kalman_breakout's
    flip, daily_long_0830's 08:00 bar. When that bar ends, the exit is
    gone. The same happens while the strategy cannot run: failed
    candles, `on_candle` raising, disabled, off the allowlist, or
    stopped by the trade-rate alarm.
  - A close that keeps filling short never escalates. Each fill ends
    the streak, so the band never widens and nothing alerts.
  - A's retry and its WARNING live in memory only
    (a-exits-never-blocked-3). After a restart the strategy is restored
    from its open-time state, so a ratcheted trailing stop rolls back
    and the exit stops firing. The streak is gone too, so no WARNING is
    logged.
- **Effect:** DB and exchange agree, so reconcile does nothing. The
  position rides until the strategy's next edge, and cdc_macd, macd_zero
  and hash_supertrend have no stop-loss. Pine's `strategy.close` would
  have executed on the next bar.
- **Owner:** NU-5 C, the engine-owned close, once its plan (#197) is
  signed off. The owed close goes out every tick, independent of
  `on_candle`, disable, allowlist and restarts, and short fills count as
  misses. NU-5d persists `export_state()` on change, so a restart keeps
  a ratcheted stop. NU-5c's close-only mode keeps a disabled,
  allowlist-dropped or rate-alarmed holder running its exits (the rest
  of exits-never-blocked-5). A's PR body qualifies its retry as "until
  the next restart".
- **Source:** `b292005` (`_PendingClose`, `_send_pending_close` and
  their tests) and `8d27bbe` (misses and delivered alerts kept across a
  restart). The C plan lists the tests to port.

## D2 · Rows larger than the exchange are never settled from fills

- **Severity:** Medium.
- **Findings:** #192 failure-injection-2, double-close-2, booking-2,
  flip-and-flat-1 (the pre-existing part), line-scan-3, cross-file-4,
  exchange-semantics-2 and alerts-logs-5 (their flat-all part),
  r1-booking-3 and its duplicate r1-flip-and-flat-3, r1-booking-5,
  r1-flip-and-flat-1, r1-tests-and-noise-1; A a-booking-and-money-1,
  a-booking-and-money-3.
- **What:**
  - **Strategy close** (failure-injection-2, a-booking-and-money-1;
    NU-5a's "mindre än raden" with a readable read). The exchange holds
    less of the row's side than the row, after a partial liquidation or
    a partial close by hand. The close is clamped to the exchange size,
    and its fill closes the whole row. The part that went earlier is
    never booked. A books such a rest only when the clamp read failed:
    the oversized reduce-only close fills what is there, and the refused
    retry books the rest.
  - **Flat-all, one order id** (booking-2, a-booking-and-money-3, and
    the flat-all part of line-scan-3, cross-file-4,
    exchange-semantics-2 and alerts-logs-5). It books every open row on
    the coin at its full size, at the order's fill price and under the
    order's one id, whatever HL filled. `trades.order_id` is unique, so
    with two rows on a coin (allow_multi_coin) the second insert fails.
    Flat-all then reports a failure on a flat book, and that row stays
    open with its strategy not reset. A row whose part was liquidated is
    booked at the flat-all price, not at its own fill. A books the size
    that filled only in reconcile pass 2 and for a coin with no row, the
    other part of those four findings.
  - **Flat-all, rows the exchange does not back** (flip-and-flat-1,
    r1-flip-and-flat-1, r1-booking-3, r1-flip-and-flat-3). Flat-all
    walks the exchange's positions, not the DB rows. An open row on a
    coin the exchange shows flat is never settled, yet the request is
    acknowledged and the bot paused. A row on the other side from the
    exchange's position (a flip by hand) is booked with that position's
    close. Whatever filled beyond the coin's rows is not booked.
  - **The rule for a gap no fill covers** (double-close-2). #192 first
    booked such a gap at the mid, with PnL and a fee on size the
    exchange never held. It must be size drift instead.
- **Effect:** the liquidated or hand-closed part's PnL never reaches
  `trades` or the daily-loss counter, or reaches them at the wrong
  price. Flat-all can fail on a flat book, or acknowledge and pause with
  rows still open. A paused reconcile is a dry run, so DB and exchange
  disagree for the whole pause.
- **Owner:** the NU-5 settlement PR ("avräkning" in the roadmap), with
  one bounded rule for the strategy's clamp and for flat-all:
  - The gap is booked from unbooked closing fills when the ledger covers
    it, with every row priced from one ledger (r1-booking-5). Otherwise
    it is size drift: pnl 0, no fee, one WARNING, no notice. Never at
    the mid, and never waiting on HL's 2,000-fill page.
  - Each row on a coin gets its own order id (`<oid>-<n>`), and a
    surplus is booked under `<oid>-surplus`.
  - A coin whose other side is open fails the attempt, and flat-all is
    acknowledged only when no open row is left on a flat coin. Both
    settle guards get a test (r1-tests-and-noise-1).
  - It builds on A's `close_row_externally` and A0's helpers.
- **Source:** `3ea78b7` (the remainder measured against the row), then
  `b292005`, `3039fe5` and `8d27bbe` (`_flat_all_close`,
  `_flat_all_book`, `_flat_all_settle_rows`, `<oid>-<n>`, the surplus,
  `test_a_partial_liquidation_is_booked_from_its_fill`). Not `47f844e`'s
  drift markers.

## D3 · A late fill is booked at a guessed price under a new id

- **Severity:** Medium.
- **Findings:** #192 booking-1, double-close-1, booking-5, tests-real-2,
  exchange-semantics-1, failure-injection-4, r1-double-close-3,
  r1-booking-4; A a-reduce-only-semantics-1, a-booking-and-money-2.
- **What:** when an order's POST times out, the audit-H2 poll
  (`_poll_for_delayed_fill`) watches the position. If it moves by the
  requested size, the order comes back FILLED under a fresh uuid.
  - On master the price is the position's entry, or the IOC limit when
    the position is gone (exchange-semantics-1, failure-injection-4).
  - After A, a reduce-only close is priced at the mid, else at the mid
    when it was sent. It carries no ESTIMATED marker, so the runner
    books it as the strategy's normal close (a-booking-and-money-2).
  - One read at the target is enough to declare it FILLED
    (r1-double-close-3).
  - The poll waits for the full requested move. When HL clips an
    oversized reduce-only close to the position, a close that filled
    comes back REJECTED "outcome unknown: timed out, no fill seen in
    30s" (a-reduce-only-semantics-1). The runner's confirmed read then
    finds the side flat and books the row as an external close.
- **Effect:** an estimated exit is booked as a real one in `trades`, the
  daily-loss counter and `/eval`. Pricing it from fills has its own
  trap: a ledger that can hand this order another order's booked fill
  (a sibling's close, a flat-all) writes a duplicate `trades.order_id`.
  The runner treats that as a DB-write divergence and pauses the bot,
  which freezes every exit (booking-1, double-close-1).
- **Owner:** the NU-5 late-fill PR (audit H2 follow-up):
  - Book a late fill at its own fill price and order id, matched from
    fills at or after the send, with the reduce-only target capped at
    the position.
  - Require two reads at the target before FILLED.
  - Give an id that is already booked a fresh id and an ESTIMATED
    reason (`Repository.late_close_id`), in the strategy close, flat-all
    and reconcile pass 2 (r1-booking-4).
  - With no fill and no mid the outcome is unknown, never the limit
    (booking-5).
  - Add a test through `place_order` and the real SDK (tests-real-2).
- **Source:** `ad3e166` (the poll's reduce-only target and its "nothing
  to reduce" case), `3039fe5` (`_late_close_price`,
  `fills.closing_orders`, `Order.late`) and `8d27bbe`
  (`Repository.late_close_id`, the two-read poll).

## D4 · allow_multi_coin: a refused close beside another row is dropped

- **Severity:** Low. It is live only while `allow_multi_coin` is on,
  which decision 5.11 rules out; the Redis flag remains.
- **Findings:** A a-reduce-only-semantics-2, a-alerts-and-logs-5.
- **What:** A's sibling guard in `_close_left_open`. When another open
  row shares the coin, or the coin's rows cannot be read, a refused or
  zero-filled close is handled as on master. A plain close is dropped:
  no re-arm, no streak, no wider band and no alert, only one WARNING
  saying the row is left open and managed by nothing. A flip re-aligns
  to its row. The guard is there because HL nets the coin. Booking at
  once could spend a liquidation fill both rows share, and a re-send
  after an unknown outcome could close the other row's position
  (#192's r1-double-close-1).
- **Effect:** the strategy has dropped the position, while the row and
  the exchange still agree, so reconcile does nothing. The live position
  is managed by no one, and no Telegram says so.
- **Owner:** the C plan's question 6 decides whether C keeps A's rule
  (its recommendation) or owns closes beside a sibling too. Either way
  the dropped close should at least alert (a-alerts-and-logs-5); no PR
  carries that yet.
- **Source:** none in #192. The guard is A's (`355859b`, reworded in
  `c4cc958`).

## D5 · A short pass-2 orphan close releases the NU-2 hold

- **Severity:** Low.
- **Findings:** #192 booking-3.
- **What:** reconcile pass 2 closes an exchange orphan. When that close
  fills short (thin liquidity inside the IOC band), the coin still
  leaves `unresolved_orphans` and the close is filed as an action, not a
  failure, so the pass counts as clean. This is pre-existing: master
  releases the hold too. A books the size that filled but keeps the
  release.
- **Effect:** the NU-2 hold is released and opens resume while part of
  the orphan is still open. A strategy's open on that coin nets into the
  rest, and pass 2 then no longer sees an orphan, because the coin has a
  row.
- **Owner:** an NU-2 follow-up of about 6 lines, plus `3039fe5`'s
  `test_an_orphan_close_that_filled_short_does_not_count`. A edits those
  lines, so it may take this if its reviewer asks.
- **Source:** `3039fe5`.

## D6 · An open can stack onto a position no row owns

- **Severity:** Low.
- **Findings:** #192 r1-tests-and-noise-3.
- **What:** the open gates (`_open_refusal`) read DB rows only. A close
  can find its own side gone and the other side held with no row, for
  example after a flip by hand. A then books the row as an external
  close and opens nothing, but nothing marks the coin. The strategy's
  next open passes every gate and lands on the unowned position.
- **Effect:** HL nets the open into that position, so the new row and
  the exchange disagree on size. Reconcile pass 2 no longer treats the
  position as an orphan, because the coin has a row.
- **Owner:** NU-5c, or its own PR.
- **Source:** `8d27bbe`: `_book_external_close` notes the coin, and
  `_open_refusal` refuses "unowned position" until a read finds it
  flat. Test: `test_a_flip_into_an_unowned_opposite_position_does_not_stack`.

## D7 · The "flip open failed" Telegram lacks `order.error`

- **Severity:** Low.
- **Findings:** #192 noise-and-logs-3 (the rest; A carries its flat-all
  and orphan parts).
- **What:** when a flip's close goes through and its open comes back
  unfilled, `_flat_after_flip_open_failed` publishes that the new open
  failed with only `order rejected` as the reason. A sets `Order.error`
  and logs it on the line before, but this Telegram drops it.
- **Effect:** from Telegram, a band rejection cannot be told from a
  margin or size refusal. The reason is only in the logs.
- **Owner:** any later order-path PR; one line.
- **Source:** `b292005`.

## D8 · Each failed HL read logs a traceback

- **Severity:** Low (log noise).
- **Findings:** #192 lifecycle-2 (the rest); A a-alerts-and-logs-6.
- **What:** two shared HL helpers log with `logger.exception`, an ERROR
  with a traceback, on every failure: `get_current_price` on a failed
  `allMids` read, and `get_positions` ("Failed to fetch user state") on
  a failed `clearinghouseState` read. Both predate NU-5a. A re-sends a
  refused close every tick, and while `clearinghouseState` fails each
  attempt makes three failing position reads; one of them,
  `_held_after_close`, is new in A. So during an HL outage every stuck
  close adds several tracebacks per tick. A already logs a reduce-only
  order's own failure lines at INFO, and no longer re-sends a close
  whose outcome is unknown.
- **Effect:** noise only, but it can bury the one line that matters
  during an outage.
- **Owner:** none; accepted noise. A fix would log a close's reads at
  INFO, as `47f844e` did for the mid (`_mid_price(symbol, INFO)`), and
  keep the helpers' level for other callers.
- **Source:** `47f844e`, for the mid read only.

## D9 · A refused close alerts on its first miss

- **Severity:** Low. This is an operator decision.
- **Findings:** #192 noise-and-logs-5; A a-never-worse-than-master-2.
- **What:** master only logged a refused close as a WARNING ("Order not
  filled"). A publishes an ErrorOccurred on the first miss, which
  reaches Telegram on mainnet; that follows the roadmap's NU-5a ("ett
  larm per (strategi, coin)"). A one-tick
  refusal that the next tick fills (no liquidity inside the 0.5 % band,
  a timed-out POST) still reaches a human. The #158 fetch-outage
  aggregator sets the opposite precedent: a window that clears before
  its threshold never notifies. Since `c4cc958` an episode ends only on
  a fill, a reset to flat or `HOLD_REALERT_SECONDS` without the exit
  firing, so the alert no longer repeats each time the exit comes and
  goes.
- **Effect:** Telegram noise for refusals that heal themselves. The
  CRITICAL alert on the first wide-band miss is not in question.
- **Owner:** the operator: alert on the first miss, as now, or only
  from the second consecutive miss and log the first at WARNING. It is
  question 10 of the C plan (#197). The order-path PR that is open when
  it is answered makes the change.
- **Source:** none; #192 never changed it.

---

## Carried by planned PRs

These deferred findings are not backlog entries. Each is part of a
planned PR's own scope, recorded in its roadmap line or plan. They are
listed so this file shows where each one went.

| PR | Findings |
|---|---|
| B (NU-5b) | exits-never-blocked-2, exits-never-blocked-4, r1-exits-never-blocked-3, flip-and-flat-2 (the pre-existing part), exits-never-blocked-6 (the notice part), r1-tests-and-noise-2, failure-injection-3 (a DB failure after a short fill, below), and master's DB-divergence auto-pause against decision 5.4 |
| NU-5c | exits-never-blocked-3 (an open gate refuses the whole flip, the only exit of hash_supertrend and kalman_breakout) and the rest of exits-never-blocked-5 |
| NU-5d | the restart part of tests-real-4 and r1-restart-and-state (a restart restores open-time state and rolls back a ratcheted stop) |
| C | r1-exits-never-blocked-1, -2 and -4, r1-restart-and-state-1 to -5, exits-never-blocked-6 (the band part); exits-money-1, exits-money-3, r1-booking-1, r1-booking-2 and r1-double-close-2 are #192 regressions that the plan's rules (a) and (b) avoid; exits-money-2, the third, is moot: A's sibling guard never classifies a close beside another row, and rule (a) keeps that guard |

**failure-injection-3 goes to B.** #192 fixed it in its first round
with two tests. A keeps
`test_a_failed_classification_still_re_arms_the_strategy` and drops
`test_a_db_failure_after_a_short_fill_still_re_arms`, which failed the
row's re-read after a short fill's write and asserted that the strategy
is re-armed on the rest. A, like master, re-syncs a short
fill to the row it read before the write, so there is no re-read to
fail. What can still fail is that read or the write. Either way the
strategy stays flat while the rest of the position is open, and a
failed write also pauses the bot. B owns DB failures after a fill, and
the split plans B to re-arm nothing after a fill. So B settles the
short-fill case: re-arm on the rest, as the dropped test asserted, or
say what manages it.

## Accepted, no owner

A behaves this way by choice. These are not § 5 records. A later PR
that takes one says so here.

- **A flip whose old side is already gone opens nothing.** #192
  line-scan-2, cross-file-3, failure-injection-6 and
  simplicity-scope-1. When a flip's close half finds its side gone and
  books the row as an external close, A resets the strategy and sends
  no open ("strategy flat, nothing opened", `_flip_close`). The split
  gives the open going ahead after such a booking to no PR and accepts
  the loss: it is an entry, not an exit, DB and exchange agree, and the
  strategy's next signal opens through every gate. An edge-triggered
  flip strategy (hash_supertrend, kalman_breakout) sits out the leg
  until its next flip. Opening safely after a booking needs what #192
  grew for it: a check that the exchange is flat and not holding an
  unowned opposite position (flip-and-flat-4), a gate-refused open that
  stays refused (r1-flip-and-flat-2), and D6's gate. The second notice
  (alerts-logs-2) is avoided: the flip's own note is not published for
  a close `_close_left_open` classified, so the external close's
  PositionClosed is the only one.
- **`_flip_refused` is moot on A** (r1-flip-and-flat-2). #192 kept a
  gate-refused flip's refusal so that its open could not go ahead after
  the close half was booked externally. A sends no open after an
  external booking, and on A a gate-refused flip sends nothing at all.
  The case returns only if the open above is ever let through.
- **An unpriceable flip close re-aligns and is re-sent while the flip
  fires** (r1-tests-and-noise-4). The flip's old side is gone, but no
  fill and no mid can price the row, so `_book_external_close` leaves it
  to reconcile. `_flip_close` then aligns as master does after any
  refused flip close: the strategy is re-synced to the row, and each
  tick the flip fires tries the reduce-only close again. It is
  refused, and each try costs the 2 s confirm read and a WARNING, with
  a "Flip-close failed" Telegram once per bar. A plain close in the
  same case leaves the strategy flat
  (`test_an_unpriceable_external_close_stays_open_for_reconcile`).
  Accepted: it needs the fills read and the mid read to fail while the
  position read works, and the first attempt that can price the row
  books it as an external close. #192 fixed it with an "unpriced"
  outcome and a 300 s retry throttle (`8d27bbe`,
  `UNBOOKED_FLIP_RETRY_SECONDS`).

## Process items, not defects

- **tests-real-info and r1-tests-and-noise-0.** Suite counts that
  #192's reviews reported.
- **lifecycle-1.** #192's PR description, rewritten for `47f844e`.
- **a-tests-real-6.** A's suite counts, and the roadmap's NU-5 "Klart
  när" bullets that no test on A covers, each with its owner: "ett
  Redis- eller DB-fel inte stoppar SL-exit" (B), "en avstängd strategi
  som håller en position stänger enligt sin regel" (NU-5c), "en omstart
  bevarar ett ratchetat SL" (NU-5d), and NU-5a's "mindre än raden" with
  a readable clamp (the settlement PR, D2). `c4cc958` routes these
  owners to A's PR Notes.
- **a-scope-and-size-1.** A is over AGENTS.md's 600-added-line budget.
  At `c4cc958` it adds 1,937 lines to the A0 base: 546 code, 17 docs
  and 1,374 tests. The tests carry the excess, because NU-5a's "Klart
  när" asks for fault-injection tests against the real Repository.
  Waiving the budget is the operator's call, argued in A's PR Notes
  (item 4).
