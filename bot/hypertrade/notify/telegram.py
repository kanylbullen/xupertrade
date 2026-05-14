"""Telegram notifier and command bot.

- Forwards selected Redis events to a Telegram chat.
- Long-polls Telegram for incoming /commands and replies with bot status.
"""

import asyncio
import html
import json
import logging
from datetime import datetime
from typing import Awaitable, Callable, Optional

import aiohttp
import re
import redis.asyncio as redis

from hypertrade.config import settings
from hypertrade.engine.control import BotControl
from hypertrade.engine.indicators_status import get_all_status
from hypertrade.events.bus import channel_for
from hypertrade.exchange.base import Exchange
from hypertrade.notify.rate_limit import check_rate_limit
from hypertrade.strategies.base import Strategy

logger = logging.getLogger(__name__)


MODE_BADGE = {
    "paper": "🟡 <b>PAPER</b>",
    "testnet": "🔵 <b>TESTNET</b>",
    "mainnet": "🟢 <b>MAINNET</b>",
}


def _mode_prefix(mode: str | None = None) -> str:
    m = mode or settings.exchange_mode
    return MODE_BADGE.get(m, f"<b>{m.upper()}</b>")


def _format_event(event: dict) -> Optional[str]:
    etype = event.get("type", "")
    ts_raw = event.get("timestamp")
    ts = ""
    if ts_raw:
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).strftime("%H:%M")
        except Exception:
            ts = ""
    mode_badge = _mode_prefix(event.get("mode"))
    prefix = f"{mode_badge} <b>{ts}</b> " if ts else f"{mode_badge} "

    if etype == "signal.generated":
        return None  # trade.executed follows immediately with the same info
    if etype == "trade.executed":
        side = str(event.get("side", "")).upper()
        emoji = "🟢" if side == "BUY" else "🔴"
        reason = html.escape(str(event.get("reason") or ""))
        msg = (
            f"{prefix}{emoji} <b>TRADE</b> {event.get('strategy')} "
            f"{side} {event.get('size')} {event.get('symbol')} "
            f"@ ${float(event.get('price', 0)):,.2f}"
        )
        if reason:
            msg += f"\n<i>{reason}</i>"
        return msg
    if etype == "position.closed":
        pnl = float(event.get("pnl", 0))
        emoji = "💰" if pnl >= 0 else "💸"
        sign = "+" if pnl >= 0 else ""
        return (
            f"{prefix}{emoji} <b>CLOSED</b> {event.get('strategy')} "
            f"{event.get('symbol')} @ ${float(event.get('exit_price', 0)):,.2f} "
            f"PnL {sign}${pnl:,.2f}"
        )
    if etype == "position.opened":
        return (
            f"{prefix}📂 <b>OPENED</b> {event.get('strategy')} "
            f"{event.get('side')} {event.get('size')} {event.get('symbol')} "
            f"@ ${float(event.get('entry_price', 0)):,.2f}"
        )
    if etype == "error":
        msg = html.escape(str(event.get("message") or ""))
        return f"{prefix}⚠️ <b>ERROR</b> {event.get('strategy')}: {msg}"
    if etype == "hodl.verdict_changed":
        asset = html.escape(str(event.get("asset") or ""))
        prev = html.escape(str(event.get("prev_verdict") or ""))
        new = html.escape(str(event.get("new_verdict") or ""))
        strat = html.escape(str(event.get("strategy") or ""))
        return (
            f"{prefix}📊 <b>HODL</b> {strat} ({asset})\n"
            f"<i>{prev} → {new}</i>"
        )
    if etype == "vault.qualified":
        name = html.escape(str(event.get("name") or ""))
        addr = str(event.get("address") or "")
        return (
            f"{prefix}🏦 <b>VAULT QUALIFIED</b> {name}\n"
            f"<i>APR {float(event.get('apr', 0))*100:.0f}% · "
            f"AUM ${float(event.get('aum_usd', 0)):,.0f} · "
            f"Sharpe {float(event.get('sharpe_180d', 0)):.2f} · "
            f"Mgr equity {float(event.get('leader_equity_pct', 0))*100:.1f}%</i>\n"
            f"<code>{addr}</code>"
        )
    if etype == "vault.disqualified":
        name = html.escape(str(event.get("name") or ""))
        failed = html.escape(str(event.get("failed_filters") or ""))
        return (
            f"{prefix}⛔ <b>VAULT DROPPED</b> {name}\n"
            f"<i>failed: {failed}</i>"
        )
    return None


CommandHandler = Callable[[list[str]], Awaitable[str]]

# Telegram /link code format (M-1 fix). Crockford-base32 minus
# 0/1/I/O, 10 chars → 32^10 ≈ 1.1×10^15 keyspace. Must match the
# alphabet used by `dashboard/src/app/api/tenant/me/telegram/link/route.ts`.
LINK_CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{10}$")
# Per-chat fixed-window (INCR + EXPIRE NX, see notify/rate_limit.py):
# 5 attempts / 30 min. With 32^10 codespace this is overkill; it just
# stops a confused legit user from hammering bad codes too. Worst case
# under fixed-window is 2× burst at the boundary (10 attempts in 30s
# at the window edge) — irrelevant given the keyspace.
LINK_RATELIMIT_MAX = 5
LINK_RATELIMIT_WINDOW_S = 30 * 60


class TelegramNotifier:
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | None = None,
        events: str | None = None,
        redis_url: str | None = None,
        control: BotControl | None = None,
        exchange: Exchange | None = None,
        strategies: list[Strategy] | None = None,
        repo=None,  # Repository | None — avoid circular import
        mainnet_control: BotControl | None = None,
    ) -> None:
        self._token = token if token is not None else settings.telegram_bot_token
        self._chat_id = chat_id if chat_id is not None else settings.telegram_chat_id
        events_raw = events if events is not None else settings.telegram_events
        self._enabled_types = {e.strip() for e in events_raw.split(",") if e.strip()}
        self._redis_url = redis_url or settings.redis_url
        self._control = control
        self._exchange = exchange
        self._strategies = strategies or []
        self._strategy_by_name = {s.name: s for s in self._strategies}
        self._repo = repo
        # Audit C4: Telegram lives on the testnet bot but its `_control` is
        # the testnet's BotControl. Mainnet's Redis keys are namespaced
        # separately (`hypertrade:mainnet:control:*`) — without an explicit
        # second handle, /pause/etc would write to testnet keys and never
        # reach mainnet. `mainnet_control` is wired in `main.py` when
        # telegram_enabled AND a mainnet bot exists in the deployment;
        # commands suffixed `-mainnet` route here.
        self._mainnet_control = mainnet_control

        self._redis: redis.Redis | None = None
        self._session: aiohttp.ClientSession | None = None
        self._event_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._daily_task: asyncio.Task | None = None
        self._weekly_task: asyncio.Task | None = None
        self._key_expiry_task: asyncio.Task | None = None
        self._last_update_id: int = 0
        self._last_command_time: dict[str, float] = {}
        self._command_cooldown_seconds: float = 5.0

        self._commands: dict[str, tuple[CommandHandler, str]] = {
            "/start": (self._cmd_start, "Show this help message"),
            "/help": (self._cmd_start, "Show this help message"),
            "/status": (self._cmd_status, "Bot status (mode, paused, equity, positions)"),
            "/strategies": (self._cmd_strategies, "All strategies + distance to trigger"),
            "/positions": (self._cmd_positions, "Open positions with unrealized PnL"),
            "/pause": (self._cmd_pause, "Pause the bot (no new signals execute)"),
            "/resume": (self._cmd_resume, "Resume the bot"),
            "/flat": (self._cmd_flat, "Close ALL open positions (with confirmation)"),
            "/today": (self._cmd_today, "Today's PnL summary (per-strategy, current mode)"),
            "/eval": (self._cmd_eval, "Weekly per-strategy evaluation (7d)"),
            "/kelly": (self._cmd_kelly, "Half-Kelly sizing report (30d, advisory only)"),
            "/link": (self._cmd_link, "Link this Telegram chat to your tenant account"),
        }
        # Mainnet variants (audit C4). Registered only when wiring is
        # actually in place — otherwise the menu would advertise commands
        # that always reply "not wired" and confuse the operator.
        if self._mainnet_control is not None:
            self._commands.update({
                "/status_mainnet": (
                    self._cmd_status_mainnet,
                    "Mainnet bot state (paused, disabled strategies, heartbeat)",
                ),
                "/pause_mainnet": (
                    self._cmd_pause_mainnet,
                    "Pause the MAINNET bot (no new signals execute)",
                ),
                "/resume_mainnet": (
                    self._cmd_resume_mainnet,
                    "Resume the MAINNET bot",
                ),
                "/flat_mainnet": (
                    self._cmd_flat_mainnet,
                    "Close ALL MAINNET positions (with confirmation)",
                ),
            })

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    async def start(self) -> None:
        if not self.configured:
            logger.info("TelegramNotifier disabled (no token / chat_id configured)")
            return
        self._redis = redis.from_url(self._redis_url, decode_responses=True)
        self._session = aiohttp.ClientSession()
        # Push the slash-command list to Telegram so the in-chat "Menu"
        # button + autocomplete shows our commands. Truly fire-and-forget
        # via create_task so a slow Telegram doesn't add even 5s to the
        # bot's cold-start path (PR #25 review fix). The function already
        # swallows + logs its own failures.
        menu_task = asyncio.create_task(self._publish_command_menu())

        def _menu_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "Telegram menu publish task died: %s",
                    exc, exc_info=(type(exc), exc, exc.__traceback__),
                )
        menu_task.add_done_callback(_menu_done)
        self._event_task = asyncio.create_task(self._event_loop())
        if self._repo is not None:
            self._daily_task = asyncio.create_task(self._daily_loop())
            self._weekly_task = asyncio.create_task(self._weekly_loop())
            if settings.tenant_id:
                self._key_expiry_task = asyncio.create_task(
                    self._key_expiry_loop()
                )
        if self._control is not None:
            self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "TelegramNotifier started (chat=%s, events=%s, commands=%s)",
            self._chat_id,
            sorted(self._enabled_types),
            self._poll_task is not None,
        )

    async def _publish_command_menu(self) -> None:
        """Register our slash-commands with Telegram so the chat's
        Menu button + autocomplete list them. One-shot per startup;
        Telegram caches the list per-bot until next setMyCommands call.

        Dedupes on description so /start and /help (same handler, same
        description) don't appear twice in the menu.

        Telegram's BotCommand.command spec requires
        ``[a-z0-9_]{1,32}``. A single invalid name (e.g. a hyphen) makes
        Telegram reject the whole batch with HTTP 400 and the menu stays
        empty — which is what bit us on PR #29 deploy. We filter
        client-side and log skipped names so a typo can't silently break
        the whole menu.
        """
        import re
        valid_cmd = re.compile(r"^[a-z0-9_]{1,32}$")

        seen_descs: set[str] = set()
        commands = []
        for cmd, (_, desc) in self._commands.items():
            if desc in seen_descs:
                continue
            name = cmd.lstrip("/")  # API expects no leading slash
            if not valid_cmd.match(name):
                logger.warning(
                    "Skipping invalid Telegram command name %r — "
                    "must match [a-z0-9_]{1,32}",
                    cmd,
                )
                continue
            seen_descs.add(desc)
            commands.append({
                "command": name,
                "description": desc[:256],   # 256 char hard limit
            })
        # Short-circuit on empty list: Telegram treats setMyCommands with
        # an empty array as "delete all commands", which would silently
        # wipe a previously-working menu if some bug filtered everything
        # out. Better to leave the existing menu in place and surface the
        # bug in the logs.
        if not commands:
            logger.warning(
                "_publish_command_menu: 0 valid commands after filtering — "
                "skipping setMyCommands call to avoid wiping the existing menu"
            )
            return
        try:
            url = f"https://api.telegram.org/bot{self._token}/setMyCommands"
            async with self._session.post(
                url, json={"commands": commands},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    if body.get("ok"):
                        logger.info(
                            "Telegram menu populated with %d commands",
                            len(commands),
                        )
                        return
                    logger.warning(
                        "setMyCommands returned ok=false: %s", body,
                    )
                else:
                    logger.warning(
                        "setMyCommands HTTP %d: %s",
                        resp.status, (await resp.text())[:200],
                    )
        except Exception:
            logger.exception(
                "Failed to publish Telegram command menu (non-fatal)"
            )

    async def stop(self) -> None:
        for t in (
            self._event_task,
            self._poll_task,
            self._daily_task,
            self._weekly_task,
            self._key_expiry_task,
        ):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if self._session:
            await self._session.close()
        if self._redis:
            await self._redis.close()

    async def send(self, text: str, to_chat_id: str | None = None) -> bool:
        """Send a message to the configured operator chat, or to a
        specific chat (used by the /link handler in PR 3b to reply
        to whoever DMed the bot — could be a tenant we haven't seen
        before, not just the operator)."""
        # /link replies need a chat_id even when no operator chat is
        # configured (token alone is enough for sending). Treat
        # configured-ness as token-only when to_chat_id is supplied.
        target = to_chat_id if to_chat_id is not None else self._chat_id
        if not self._token or not self._session or not target:
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with self._session.post(
                url,
                json={
                    "chat_id": target,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Telegram send failed: %s %s", resp.status, await resp.text()
                    )
                    return False
                return True
        except Exception:
            logger.exception("Telegram send error")
            return False

    # ----- event forwarding -----

    async def _event_loop(self) -> None:
        assert self._redis is not None
        pubsub = self._redis.pubsub()
        # Subscribe to all 3 modes' event channels so a single Telegram bot
        # can receive notifications regardless of which mode publishes them.
        channels = [channel_for(m) for m in ("paper", "testnet", "mainnet")]
        await pubsub.subscribe(*channels)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    event = json.loads(message.get("data") or "{}")
                except json.JSONDecodeError:
                    continue
                etype = event.get("type", "")
                if self._enabled_types and etype not in self._enabled_types:
                    continue
                text = _format_event(event)
                if text:
                    await self.send(text)
        finally:
            await pubsub.unsubscribe()
            await pubsub.close()

    # ----- command polling -----

    async def _poll_loop(self) -> None:
        assert self._session is not None
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        backoff = 1.0
        while True:
            try:
                async with self._session.get(
                    url,
                    params={
                        "offset": self._last_update_id + 1,
                        "timeout": 25,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.warning("getUpdates failed: %s %s", resp.status, text)
                        await asyncio.sleep(min(backoff, 30))
                        backoff *= 2
                        continue
                    data = await resp.json()
                backoff = 1.0
                for update in data.get("result", []):
                    self._last_update_id = max(self._last_update_id, update.get("update_id", 0))
                    msg = update.get("message")
                    if not msg:
                        continue
                    chat = msg.get("chat", {})
                    chat_id = str(chat.get("id", ""))
                    from_user = msg.get("from", {}) or {}
                    username = from_user.get("username")
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    # Tenant-linking exception (PR 3b): /link from any
                    # chat is allowed so a new tenant can DM the bot
                    # before they're known to us. Every other command
                    # stays gated to the operator's configured chat
                    # (back-compat). The /link handler itself is the
                    # auth boundary — it validates the code against
                    # Redis-stored tenant_id before doing anything.
                    is_link_cmd = text.split()[0].split("@")[0].lower() == "/link"
                    if not is_link_cmd and chat_id != str(self._chat_id):
                        continue
                    await self._handle_command(text, chat_id=chat_id, username=username)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram poll loop error")
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2

    async def _handle_command(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        username: str | None = None,
    ) -> None:
        import time as _time
        # Strip @botname suffix from /command@botname
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]

        # /link has its own per-chat fixed-window throttle (M-1).
        # The legacy global-cooldown bucket is keyed by command name
        # only, so a single brute-forcer would throttle legitimate
        # tenants out of the feature — keep it for everything ELSE
        # (status/positions/etc) where global pacing is fine.
        if cmd != "/link":
            now = _time.monotonic()
            last = self._last_command_time.get(cmd, 0.0)
            if now - last < self._command_cooldown_seconds:
                return
            self._last_command_time[cmd] = now

        handler_entry = self._commands.get(cmd)
        if not handler_entry:
            await self.send(
                f"Unknown command: <code>{cmd}</code>\n\nTry /help",
                to_chat_id=chat_id,
            )
            return
        handler, _ = handler_entry
        try:
            # /link needs the message's chat_id + username to do
            # its job (storing them in tenant_telegram_links). All
            # other handlers ignore the kwargs — Python's
            # accept-but-ignore via **_ would work but we pass
            # explicitly so type signatures stay honest. The
            # `cmd == "/link"` branch is the only special path.
            if cmd == "/link":
                reply = await handler(args, chat_id=chat_id, username=username)
            else:
                reply = await handler(args)
        except Exception as e:
            logger.exception("Command handler failed: %s", cmd)
            reply = f"⚠️ Command failed: <code>{e}</code>"
        if reply:
            # Reply in the originating chat (operator default OR
            # tenant for /link).
            await self.send(reply, to_chat_id=chat_id)

    # ----- daily summary -----

    async def _daily_loop(self) -> None:
        """Send a daily PnL summary at 23:00 Europe/Stockholm time."""
        from datetime import datetime, timedelta, timezone
        # Stockholm offset is +1 (winter) or +2 (summer). Resolve via system tz
        # if available, else assume +1.
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Stockholm")
        except Exception:
            tz = timezone(timedelta(hours=1))

        while True:
            now = datetime.now(tz)
            target = now.replace(hour=23, minute=0, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            sleep_seconds = (target - now).total_seconds()
            try:
                await asyncio.sleep(sleep_seconds)
            except asyncio.CancelledError:
                raise
            try:
                summary = await self._build_daily_summary()
                if summary:
                    await self.send(summary)
            except Exception:
                logger.exception("Daily summary failed")

    async def _build_daily_summary(self) -> str | None:
        """Aggregate today's trades and produce a Telegram-formatted digest."""
        if self._repo is None:
            return None
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(hours=24)
        trades = await self._repo.get_trades_since(since)
        if not trades:
            return f"{_mode_prefix()} 📊 <b>Daily summary</b>\nNo trades in the last 24h."

        # Aggregate per strategy
        per_strat: dict[str, dict] = {}
        total_pnl = 0.0
        total_fees = 0.0
        wins = 0
        losses = 0
        for t in trades:
            s = per_strat.setdefault(
                t.strategy_name,
                {"trades": 0, "pnl": 0.0, "fees": 0.0, "wins": 0, "losses": 0},
            )
            s["trades"] += 1
            s["fees"] += float(t.fee or 0)
            total_fees += float(t.fee or 0)
            if t.pnl is not None:
                pnl = float(t.pnl)
                s["pnl"] += pnl
                total_pnl += pnl
                if pnl > 0:
                    s["wins"] += 1
                    wins += 1
                elif pnl < 0:
                    s["losses"] += 1
                    losses += 1

        net = total_pnl
        emoji = "💰" if net >= 0 else "💸"
        sign = "+" if net >= 0 else ""

        lines = [
            f"{_mode_prefix()} {emoji} <b>Daily summary</b> (24h)",
            f"Net PnL: <b>{sign}${net:,.2f}</b>",
            f"Trades: {len(trades)} ({wins}W / {losses}L)",
            f"Fees paid: ${total_fees:,.2f}",
            "",
            "<b>Per strategy:</b>",
        ]

        # Sort by PnL descending
        sorted_strats = sorted(
            per_strat.items(), key=lambda kv: kv[1]["pnl"], reverse=True
        )
        for name, s in sorted_strats:
            sign_s = "+" if s["pnl"] >= 0 else ""
            wl = f"{s['wins']}W/{s['losses']}L" if (s["wins"] + s["losses"]) else f"{s['trades']}t"
            lines.append(
                f"  <code>{name}</code>: {sign_s}${s['pnl']:,.2f} ({wl})"
            )

        return "\n".join(lines)

    async def _weekly_loop(self) -> None:
        """Send a weekly per-strategy evaluation on Sunday at 18:00 local time."""
        from datetime import datetime, timedelta, timezone
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Europe/Stockholm")
        except Exception:
            tz = timezone(timedelta(hours=1))

        while True:
            now = datetime.now(tz)
            # Sunday is weekday 6. Aim for next Sunday 18:00 local.
            days_ahead = (6 - now.weekday()) % 7
            target = (now + timedelta(days=days_ahead)).replace(
                hour=18, minute=0, second=0, microsecond=0
            )
            if target <= now:
                target = target + timedelta(days=7)
            try:
                await asyncio.sleep((target - now).total_seconds())
            except asyncio.CancelledError:
                raise
            try:
                from hypertrade.reports.weekly_eval import evaluate, format_summary_text
                names = [s.name for s in self._strategies]
                stats = await evaluate(self._repo, names, days=7)
                text = format_summary_text(stats, days=7, html=True)
                await self.send(text)
            except Exception:
                logger.exception("Weekly eval failed")

    # ----- HL key expiry reminders -----

    REMINDER_WINDOW_DAYS = 14
    EXPIRED_NOTIFIED_TTL_SECONDS = 30 * 24 * 3600

    async def _key_expiry_loop(self) -> None:
        """Daily check: warn about HL private keys nearing/past expiry.

        Runs once per day at 09:00 UTC. Picks up `expires_at` set on
        the two HL private-key rows in `tenant_secrets`. Sends a
        recurring 14-day warning + a one-shot EXPIRED notification
        deduped via Redis.
        """
        from datetime import datetime, timedelta, timezone

        while True:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if target <= now:
                target = target + timedelta(days=1)
            try:
                await asyncio.sleep((target - now).total_seconds())
            except asyncio.CancelledError:
                raise
            try:
                await self._check_key_expiries()
            except Exception:
                logger.exception("Key expiry check failed")

    async def _check_key_expiries(self) -> None:
        if (
            self._repo is None
            or self._redis is None
            or not settings.tenant_id
            or not self.configured
        ):
            return
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone

        try:
            tenant_uuid = _uuid.UUID(settings.tenant_id)
        except (ValueError, AttributeError):
            return
        rows = await self._repo.get_hl_key_expiries(tenant_uuid)
        now = datetime.now(timezone.utc)
        window = now + timedelta(days=self.REMINDER_WINDOW_DAYS)
        for key, expires_at in rows:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if now < expires_at <= window:
                days = max(0, (expires_at - now).days)
                date_iso = expires_at.date().isoformat()
                await self.send(
                    f"🔑 HL key <code>{html.escape(key)}</code> expires in "
                    f"<b>{days}</b> days ({date_iso}). Rotate it via "
                    f"Settings → Credentials."
                )
            elif now >= expires_at:
                date_iso = expires_at.date().isoformat()
                dedup_key = (
                    f"tenant:{settings.tenant_id}:secret_expired_notified:"
                    f"{key}:{expires_at.isoformat()}"
                )
                # SET NX so the first daily pass after expiry sends and
                # subsequent passes are no-ops. TTL auto-cleans the
                # marker so a future expires_at change (different ISO)
                # gets its own dedup slot.
                marked = await self._redis.set(
                    dedup_key, "1",
                    nx=True, ex=self.EXPIRED_NOTIFIED_TTL_SECONDS,
                )
                if marked:
                    await self.send(
                        f"⚠️ HL key <code>{html.escape(key)}</code> "
                        f"EXPIRED on {date_iso}. Bot cannot trade with "
                        f"this key. Rotate now."
                    )

    # ----- command handlers -----

    async def _cmd_start(self, _args: list[str]) -> str:
        lines = [f"{_mode_prefix()} <b>xupertrade bot — commands</b>"]
        seen = set()
        for cmd, (_, desc) in self._commands.items():
            if desc in seen:
                continue
            seen.add(desc)
            lines.append(f"<code>{cmd}</code> — {desc}")
        return "\n".join(lines)

    async def _cmd_status(self, _args: list[str]) -> str:
        if not self._exchange or not self._control:
            return "Status unavailable (control/exchange not wired)"
        balance = await self._exchange.get_balance()
        positions = await self._exchange.get_positions()
        paused = await self._control.is_paused()
        disabled = sorted(await self._control.get_disabled_strategies())
        active = [s.name for s in self._strategies if s.name not in disabled]

        return (
            f"{_mode_prefix()} <b>status</b>\n"
            f"State: {'⏸ <b>PAUSED</b>' if paused else '▶ running'}\n"
            f"Equity: <b>${balance.total:,.2f}</b> "
            f"(unrealized {'+' if balance.unrealized_pnl >= 0 else ''}${balance.unrealized_pnl:,.2f})\n"
            f"Open positions: {len(positions)}\n"
            f"Active strategies: {', '.join(active) if active else 'none'}"
            + (f"\nDisabled: {', '.join(disabled)}" if disabled else "")
        )

    async def _cmd_strategies(self, _args: list[str]) -> str:
        try:
            statuses = await get_all_status()
        except Exception as e:
            return f"Failed to fetch strategy status: <code>{e}</code>"

        overrides = {}
        if self._control:
            overrides = await self._control.get_all_leverage_overrides()
        disabled = set()
        if self._control:
            disabled = await self._control.get_disabled_strategies()

        signal_emoji = {
            "long": "🟦",
            "short": "🟥",
            "ready_long": "🟢",
            "ready_short": "🔴",
            "flat": "⚪",
        }
        lines = [f"{_mode_prefix()} <b>Strategies</b>"]
        for s in statuses:
            strat = self._strategy_by_name.get(s.name)
            lev = overrides.get(s.name, strat.leverage if strat else 1)
            on_off = "🚫" if s.name in disabled else "✅"
            emoji = signal_emoji.get(s.signal, "·")
            lines.append(
                f"\n{on_off} <b>{s.name}</b> ({s.symbol} {s.timeframe}, {lev}x) {emoji} {s.signal}"
            )
            lines.append(f"  {html.escape(s.description[:140])}")
        return "\n".join(lines)

    async def _cmd_positions(self, _args: list[str]) -> str:
        if not self._exchange:
            return "Exchange unavailable"
        positions = await self._exchange.get_positions()
        if not positions:
            return f"{_mode_prefix()} No open positions."
        lines = [f"{_mode_prefix()} <b>Open positions</b>"]
        for p in positions:
            pnl = p.unrealized_pnl
            sign = "+" if pnl >= 0 else ""
            lines.append(
                f"  {p.side.upper()} {p.size} <b>{p.symbol}</b> @ ${p.entry_price:,.2f} "
                f"PnL {sign}${pnl:,.2f}"
            )
        return "\n".join(lines)

    async def _cmd_pause(self, _args: list[str]) -> str:
        if not self._control:
            return "Control unavailable"
        await self._control.set_paused(True)
        return f"{_mode_prefix()} ⏸ Bot paused. No new signals will execute."

    async def _cmd_resume(self, _args: list[str]) -> str:
        if not self._control:
            return "Control unavailable"
        await self._control.set_paused(False)
        return f"{_mode_prefix()} ▶ Bot resumed."

    async def _cmd_today(self, _args: list[str]) -> str:
        summary = await self._build_daily_summary()
        return summary or "Daily summary unavailable (DB not configured)"

    async def _cmd_eval(self, args: list[str]) -> str:
        if self._repo is None:
            return "Evaluation unavailable (DB not configured)"
        days = 7
        if args and args[0].isdigit():
            days = max(1, min(90, int(args[0])))
        from hypertrade.reports.weekly_eval import evaluate, format_summary_text
        names = [s.name for s in self._strategies]
        stats = await evaluate(self._repo, names, days=days)
        return format_summary_text(stats, days=days, html=True)

    async def _cmd_kelly(self, args: list[str]) -> str:
        if self._repo is None:
            return "Kelly report unavailable (DB not configured)"
        days = 30  # Kelly needs more data than the 7d default
        if args and args[0].isdigit():
            days = max(7, min(180, int(args[0])))
        from hypertrade.reports.weekly_eval import evaluate, format_kelly_report
        names = [s.name for s in self._strategies]
        stats = await evaluate(self._repo, names, days=days)
        return format_kelly_report(stats, days=days, html=True)

    async def _cmd_link(
        self,
        args: list[str],
        *,
        chat_id: str | None = None,
        username: str | None = None,
    ) -> str:
        """`/link <code>` — pair this Telegram chat with a tenant
        account. Code comes from POST /api/tenant/me/telegram/link
        on the dashboard side; it's a 10-character Crockford-base32
        string (M-1: widened from the original 6-digit format) and
        lives in Redis with a 10-min TTL.

        Auth: the code IS the auth — only the tenant who minted it
        knows the value, and consumption is one-shot. Re-link
        attempts for the same tenant just overwrite, so a user
        switching devices doesn't get stuck.

        Per-chat rate-limit: 5 attempts / 30 min, hard-fail beyond.
        Defence-in-depth on top of the 32^10 codespace — a single
        attacker can't hammer one chat looking for code reuse.
        """
        if self._repo is None or self._redis is None:
            return "⚠️ Linking unavailable (DB or Redis not configured)"
        if chat_id is None:
            return "⚠️ Internal error: missing chat context"

        # Per-chat fixed-window (M-1). Run BEFORE format check so a
        # spam of "/link garbage" still counts toward the limit — we
        # don't want to give attackers a free probe channel by
        # rejecting on format and skipping the counter.
        rl = await check_rate_limit(
            self._redis,
            "tg-link-attempt",
            chat_id,
            max_events=LINK_RATELIMIT_MAX,
            window_seconds=LINK_RATELIMIT_WINDOW_S,
        )
        if not rl.allowed:
            minutes = max(1, (rl.reset_in_seconds + 59) // 60)
            return (
                f"❌ Too many /link attempts from this chat. "
                f"Try again in <b>{minutes}</b> min."
            )

        # Accept case-insensitive; strip stray whitespace so users
        # copying from the dashboard don't trip on trailing spaces.
        raw = (args[0].strip().upper() if args else "")
        # Old 6-digit codes are no longer accepted. They have a
        # 10-min TTL so any in-flight code from before the upgrade
        # has already expired by the time the new bot ships, but
        # surface a clear message just in case.
        if raw.isdigit() and len(raw) == 6:
            return (
                "❌ The 6-digit code format is no longer supported.\n\n"
                "Mint a fresh code on the dashboard's "
                "Settings → Credentials page — it'll be 10 characters."
            )
        if len(args) != 1 or not LINK_CODE_RE.match(raw):
            return (
                "Usage: <code>/link ABCDE2HJK7</code>\n\n"
                "Get your 10-character code from the dashboard's "
                "Settings → Credentials page (case-insensitive, "
                "characters A-Z minus I/O and digits 2-9 — no 0, 1)."
            )
        code = raw
        key = f"tg-link:{code}"
        # GETDEL is atomic — read-and-delete in one Redis op. Without
        # this, two concurrent /link with the same code could both
        # read tenant_id and both upsert (which the schema's UNIQUE
        # chat_id catches, but only one survives — the loser sees a
        # confusing DB error). With GETDEL, the loser sees None and
        # gets the friendly "code invalid or expired" message.
        tenant_id_str = await self._redis.getdel(key)
        if not tenant_id_str:
            return (
                "❌ Code invalid or expired.\n\n"
                "Generate a fresh code on the dashboard's "
                "Settings → Credentials page."
            )

        import uuid as _uuid
        try:
            tenant_id = _uuid.UUID(tenant_id_str)
        except ValueError:
            logger.error("Malformed tenant_id in Redis under %s: %r", key, tenant_id_str)
            return "⚠️ Internal error: linking state corrupted, please retry"

        try:
            await self._repo.upsert_telegram_link(
                tenant_id=tenant_id,
                telegram_chat_id=int(chat_id),
                telegram_username=username,
            )
        except Exception:
            logger.exception("upsert_telegram_link failed for %s", tenant_id)
            return "⚠️ Database error while linking. Please retry."

        # Forward key was already consumed by GETDEL above.
        # Just clean up the reverse pointer so a re-mint gives a
        # fresh number rather than returning the now-stale code.
        try:
            await self._redis.delete(f"tg-link:tenant:{tenant_id}")
        except Exception:
            logger.exception("Failed to clean up tg-link reverse pointer")

        return (
            "✅ Linked!\n\n"
            "You'll get unlock notifications here when your bot needs "
            "your passphrase after a restart."
        )

    async def _cmd_flat(self, args: list[str]) -> str:
        if not self._control:
            return "Control unavailable"
        if not args or args[0].lower() != "confirm":
            positions = await self._exchange.get_positions() if self._exchange else []
            return (
                f"{_mode_prefix()} ⚠️ This will close <b>{len(positions)}</b> open positions.\n"
                f"Reply with <code>/flat confirm</code> to proceed."
            )
        import uuid

        token = uuid.uuid4().hex
        await self._control.request_flat_all(token)
        return f"{_mode_prefix()} ✅ Flat-all requested (token <code>{token[:8]}…</code>)."

    # --- Mainnet variants (audit C4) ---------------------------------------
    # Each delegates to the testnet implementation pattern, but writes to
    # the MAINNET-namespaced Redis keys so the mainnet bot actually sees
    # the request. Without these, the operator's `/pause` from Telegram
    # silently writes to testnet's keys; mainnet keeps trading.

    async def _cmd_status_mainnet(self, _args: list[str]) -> str:
        if not self._mainnet_control:
            return "Mainnet control not wired (no mainnet bot in this deployment)"
        paused = await self._mainnet_control.is_paused()
        disabled = sorted(await self._mainnet_control.get_disabled_strategies())
        hb = await self._mainnet_control.get_heartbeat()
        import time as _time
        if hb is None:
            hb_msg = "❓ no heartbeat"
        else:
            age = int(_time.time() - hb)
            hb_msg = f"{age}s ago" if age < 180 else f"⚠️ stale ({age}s ago)"
        return (
            f"{MODE_BADGE['mainnet']} <b>status</b>\n"
            f"State: {'⏸ <b>PAUSED</b>' if paused else '▶ running'}\n"
            f"Heartbeat: {hb_msg}\n"
            + (f"Disabled: {', '.join(disabled)}" if disabled else "Disabled: none")
        )

    async def _cmd_pause_mainnet(self, _args: list[str]) -> str:
        if not self._mainnet_control:
            return "Mainnet control not wired (no mainnet bot in this deployment)"
        await self._mainnet_control.set_paused(True)
        return f"{MODE_BADGE['mainnet']} ⏸ MAINNET paused. No new signals will execute."

    async def _cmd_resume_mainnet(self, _args: list[str]) -> str:
        if not self._mainnet_control:
            return "Mainnet control not wired (no mainnet bot in this deployment)"
        await self._mainnet_control.set_paused(False)
        return f"{MODE_BADGE['mainnet']} ▶ MAINNET resumed."

    async def _cmd_flat_mainnet(self, args: list[str]) -> str:
        if not self._mainnet_control:
            return "Mainnet control not wired (no mainnet bot in this deployment)"
        if not args or args[0].lower() != "confirm":
            return (
                f"{MODE_BADGE['mainnet']} ⚠️ This will close ALL open MAINNET "
                f"positions on the live exchange.\n"
                f"Reply with <code>/flat_mainnet confirm</code> to proceed."
            )
        import uuid

        token = uuid.uuid4().hex
        await self._mainnet_control.request_flat_all(token)
        return (
            f"{MODE_BADGE['mainnet']} ✅ MAINNET flat-all requested "
            f"(token <code>{token[:8]}…</code>)."
        )
