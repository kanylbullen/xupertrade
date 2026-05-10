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
import redis.asyncio as redis

from hypertrade.config import settings
from hypertrade.engine.control import BotControl
from hypertrade.engine.indicators_status import get_all_status
from hypertrade.events.bus import channel_for
from hypertrade.exchange.base import Exchange
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
            # Mainnet variants (audit C4). Available only when this bot was
            # constructed with a mainnet_control handle. The handlers
            # themselves return "Mainnet control not wired" if not.
            "/status-mainnet": (
                self._cmd_status_mainnet,
                "Mainnet bot state (paused, disabled strategies, heartbeat)",
            ),
            "/pause-mainnet": (
                self._cmd_pause_mainnet,
                "Pause the MAINNET bot (no new signals execute)",
            ),
            "/resume-mainnet": (
                self._cmd_resume_mainnet,
                "Resume the MAINNET bot",
            ),
            "/flat-mainnet": (
                self._cmd_flat_mainnet,
                "Close ALL MAINNET positions (with confirmation)",
            ),
        }

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
        """
        seen_descs: set[str] = set()
        commands = []
        for cmd, (_, desc) in self._commands.items():
            if desc in seen_descs:
                continue
            seen_descs.add(desc)
            commands.append({
                "command": cmd.lstrip("/"),  # API expects no leading slash
                "description": desc[:256],   # 256 char hard limit
            })
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
        for t in (self._event_task, self._poll_task, self._daily_task, self._weekly_task):
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

    async def send(self, text: str) -> bool:
        if not self.configured or not self._session:
            return False
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with self._session.post(
                url,
                json={
                    "chat_id": self._chat_id,
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
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != str(self._chat_id):
                        # Ignore messages from anyone but the configured chat
                        continue
                    text = (msg.get("text") or "").strip()
                    if not text.startswith("/"):
                        continue
                    await self._handle_command(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram poll loop error")
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2

    async def _handle_command(self, text: str) -> None:
        import time as _time
        # Strip @botname suffix from /command@botname
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]

        now = _time.monotonic()
        last = self._last_command_time.get(cmd, 0.0)
        if now - last < self._command_cooldown_seconds:
            return
        self._last_command_time[cmd] = now

        handler_entry = self._commands.get(cmd)
        if not handler_entry:
            await self.send(f"Unknown command: <code>{cmd}</code>\n\nTry /help")
            return
        handler, _ = handler_entry
        try:
            reply = await handler(args)
        except Exception as e:
            logger.exception("Command handler failed: %s", cmd)
            reply = f"⚠️ Command failed: <code>{e}</code>"
        if reply:
            await self.send(reply)

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

    # ----- command handlers -----

    async def _cmd_start(self, _args: list[str]) -> str:
        lines = [f"{_mode_prefix()} <b>HyperTrade bot — commands</b>"]
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
                f"Reply with <code>/flat-mainnet confirm</code> to proceed."
            )
        import uuid

        token = uuid.uuid4().hex
        await self._mainnet_control.request_flat_all(token)
        return (
            f"{MODE_BADGE['mainnet']} ✅ MAINNET flat-all requested "
            f"(token <code>{token[:8]}…</code>)."
        )
