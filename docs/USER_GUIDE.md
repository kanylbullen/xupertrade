# xupertrade — beta user guide

You've been invited to a private `xupertrade` instance. This guide takes
you from first sign-in to a running bot, and is honest about what is and
isn't finished.

If something here doesn't match what you see on screen, the guide is
wrong — tell the operator.

> **Read this first.** This software places real orders on
> HyperLiquid. Start on **paper**, graduate to **testnet**, and only
> consider **mainnet** once you have watched a strategy behave for
> days. No strategy in this system is proven profitable — see
> [Managing expectations](#managing-expectations).

---

## 1. Sign in

Browse to the dashboard URL your operator gave you. You'll be redirected
to **Authentik** to sign in, then back to the dashboard.

There's no sign-up form. Access is granted by the operator adding your
Authentik account to a group. If sign-in bounces you back to the login
page, your account isn't in that group yet — ask the operator.

On your first successful sign-in the dashboard creates your tenant
account automatically. Nothing else is shared with other users.

---

## 2. Set your passphrase

The dashboard immediately asks for a **passphrase**. This is *not* your
Authentik password, and the operator never sees it.

It's the encryption key for every credential you store. The dashboard
derives a key from it (Argon2id), keeps only a salt and a verifier, and
never writes the key itself to disk.

- Minimum 12 characters.
- **There is no recovery.** Not by the operator, not by anyone.

If you lose it, your stored credentials become permanently unreadable
and you start over by re-entering them. Your trade history survives —
only the credentials are encrypted.

**Put it in a password manager before continuing.**

---

## 3. Get a HyperLiquid API wallet

Do **not** use the private key of a wallet holding your funds.

HyperLiquid lets you generate an **API wallet**: a separate keypair,
authorised by your main wallet, that can place and cancel orders **but
cannot withdraw**. That's what you give the bot.

1. Testnet: <https://app.hyperliquid-testnet.xyz/API>
   Mainnet: <https://app.hyperliquid.xyz/API>
2. Generate an API wallet. You get an address and a private key.
3. Authorise it with your main wallet.
4. Copy the **API wallet's private key**, and separately your **main
   wallet's address**.

API wallets expire after **180 days**. The dashboard has a field for the
expiry date and will remind you over Telegram starting 14 days out — fill
it in, it's the only thing standing between you and a bot that silently
stops signing.

---

## 4. Store your credentials

**Settings → Credentials.** You'll need at least:

| Field | What it is |
|---|---|
| `HYPERLIQUID_PRIVATE_KEY` | Your **API wallet's** private key (starts `0x`) |
| `HYPERLIQUID_ACCOUNT_ADDRESS` | Your **main wallet's** address — the account the bot trades on behalf of |

Optional, for notifications:

| Field | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Create your own bot via [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | Your own user ID via [@userinfobot](https://t.me/userinfobot) |

Use your **own** Telegram bot, not a shared one. Your bot only ever
messages the chat ID you configure.

### Why it's safe to paste a private key here

Reasonable question. The short version:

- The value travels from your browser to the server over TLS, is
  encrypted **in memory** under your passphrase-derived key, and only
  the ciphertext reaches the database.
- Plaintext never touches durable storage — not the database, not the
  secrets manager, not the logs, not container images.
- Someone with database access but not your passphrase sees only
  ciphertext.
- The API wallet **cannot withdraw funds** even if it were exposed. That
  is the real backstop, and it's why step 3 matters.

**What this does not protect against:** the operator has root on the
host. A running bot must hold the plaintext key in memory to sign
orders — unavoidable for any non-custodial trading system — so root can
read it out of process memory or container environment. Trust the
operator to that extent, or don't participate. The full model is in the
[README](../README.md#security-model--credentials-at-rest).

---

## 5. Start a bot

**Settings → Bots.**

1. Pick a mode:
   - **paper** — simulated exchange, fake money. Start here.
   - **testnet** — real HyperLiquid testnet orders, fake money.
   - **mainnet** — **real money**.
2. Click **Start**. You'll be asked to unlock with your passphrase so
   the dashboard can decrypt your credentials and inject them into your
   bot's container.

Unless the operator has enabled multi-bot for you, you get **one bot at
a time** — stop the current one before switching mode.

Your bot is an isolated container with its own database role. It can
only see your rows.

### If Start fails

| Symptom | Cause |
|---|---|
| "missing required secret" | A credential for that mode isn't stored yet |
| Unlock rejected | Wrong passphrase — there's no reset, retype carefully |
| Bot starts then stops | Usually a bad private key or unauthorised API wallet. Check **Status**. |

---

## 6. What the pages do

| Page | Purpose |
|---|---|
| **Overview** | Per-mode P&L, equity curve, open positions, recent trades |
| **Trades** | Full history. Filter by mode, strategy and date range; paginated |
| **Strategies** | Reference for every strategy — logic, strengths, weaknesses |
| **Status** | Bot health, heartbeat, indicator state per strategy |
| **Vaults** / **HODL** | Advisory only. Never place orders. |
| **Settings → Bots** | Start/stop, pause, per-strategy toggles, leverage |
| **Settings → Credentials** | Add, replace, delete credentials; set key expiry |

Your operator may restrict which strategies you can run, and how many
can be active at once. If a strategy is missing or won't enable, that's
the operator's limit, not a bug.

---

## 7. Lock, sign out, stop

These are three different things and the difference matters:

- **Lock** — clears your decryption key from the server's cache. Your
  bot **keeps trading**; it already holds its credentials.
- **Sign out** — ends your dashboard session. Your bot **keeps
  trading**.
- **Stop bot** — actually stops trading, and removes the container.

**Closing the browser does not stop your bot.** If you want it to stop,
stop it.

---

## 8. Managing expectations

Be blunt with yourself about this part.

- **No strategy here is proven profitable.** Most were ported from
  TradingView scripts and backtested over a limited window. Several have
  been disabled for consistently losing money.
- **Backtests overstate reality.** Fees, funding and slippage are
  modelled, not measured.
- **Paper mode is optimistic** — fills are simulated and always
  succeed.
- Run paper for days, then testnet for longer. Only risk real money you
  are prepared to lose entirely.

The system's actual guarantee is narrower than "it makes money": it
executes its strategies faithfully, keeps its database in step with the
exchange, survives restarts without losing position state, and tells you
loudly when something breaks.

---

## 9. Known beta limitations

Current, honest list:

- **Live updates poll rather than stream.** The event stream is
  operator-only for now, because bot events don't yet carry a tenant ID
  that would let it be filtered safely per user.
- **No tenant cap.** The operator polices headcount manually, so
  performance depends on how many people share the host.
- **No self-service account deletion.** Ask the operator; it's a manual
  database operation.
- **No passphrase recovery.** Stated again because it is the single
  most common way to lose your setup.
- **API-wallet expiry reminders need Telegram.** No Telegram
  credentials means no reminder — and a silently expired key looks like
  a bot that just stopped trading.

---

## 10. Reporting problems

Include:

1. What you did, what you expected, what happened.
2. Mode (paper / testnet / mainnet) and roughly when, with timezone.
3. Strategy name, if it's about a specific one.
4. Whatever **Status** shows.

**Never paste a private key, passphrase or Telegram token into a bug
report.** If you think you've leaked a key: generate a new API wallet on
HyperLiquid immediately, which invalidates the old one, then replace it
under Settings → Credentials and restart your bot.
