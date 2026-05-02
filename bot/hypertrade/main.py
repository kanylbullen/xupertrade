"""HyperTrade — entry point."""

import asyncio
import logging
import signal

from hypertrade.api import start_api_server
from hypertrade.config import settings
from hypertrade.data.feed import HyperLiquidWebSocket
from hypertrade.db.repo import Repository
from hypertrade.engine.control import BotControl
from hypertrade.engine.runner import EngineRunner
from hypertrade.notify.telegram import TelegramNotifier
from hypertrade.events.bus import EventBus, NoOpEventBus
from hypertrade.exchange.paper import PaperExchange
from hypertrade.strategies.registry import get_strategy, list_strategies, load_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("hypertrade")

_shutdown = asyncio.Event()


def _handle_signal(*_: object) -> None:
    logger.info("Shutdown signal received")
    _shutdown.set()


async def main() -> None:
    # Register strategies
    load_all()
    available = list_strategies()
    logger.info("Available strategies: %s", available)

    # Set up exchange
    if settings.is_paper:
        exchange = PaperExchange(settings.paper_initial_balance)
        logger.info("Running in PAPER mode (balance: $%.2f)", settings.paper_initial_balance)
    elif settings.is_testnet:
        from hypertrade.exchange.hyperliquid import HyperLiquidExchange

        exchange = HyperLiquidExchange()
        logger.info("Running in TESTNET mode (HyperLiquid testnet, real orders, fake money)")
    elif settings.is_mainnet:
        from hypertrade.exchange.hyperliquid import HyperLiquidExchange

        exchange = HyperLiquidExchange()
        logger.warning("Running in MAINNET mode (REAL MONEY) — risk limits apply")
    else:
        raise ValueError(
            f"Unknown EXCHANGE_MODE: {settings.exchange_mode!r}. "
            "Must be one of: paper, testnet, mainnet"
        )

    # Set up DB
    repo: Repository | None = None
    try:
        repo = Repository()
        await repo.init_db()
    except Exception:
        logger.warning("Database unavailable — running without persistence")
        repo = None

    # Reconcile DB positions with exchange reality on startup.
    # Closes orphan rows (DB says open, exchange says no position) and rows
    # whose side disagrees with the exchange. Size mismatches are logged.
    if repo is not None:
        try:
            actions = await repo.reconcile_positions(exchange)
            if actions:
                logger.warning(
                    "Reconcile: cleaned up %d stale DB position(s) on startup",
                    len(actions),
                )
        except Exception:
            logger.exception("Reconcile failed (continuing without it)")

    # Set up event bus
    event_bus: EventBus
    try:
        event_bus = EventBus()
        await event_bus.connect()
    except Exception:
        logger.warning("Redis unavailable — running without events")
        event_bus = NoOpEventBus()

    # Bot control (pause/resume/flat-all/per-strategy toggle)
    control: BotControl | None = None
    try:
        control = BotControl()
        await control.connect()
    except Exception:
        logger.warning("BotControl unavailable — running without runtime controls")
        control = None

    # Telegram notifier (optional) — wired with control/exchange/strategies
    # later, after they're constructed
    telegram: TelegramNotifier | None = None

    # Instantiate every registered strategy. Disabling at runtime is done
    # via Redis (BotControl.disable_strategy) — Options page or Telegram
    # /strategies. New strategies added under hypertrade/strategies/ and
    # imported in registry.load_all() automatically appear here.
    strategies = [get_strategy(name) for name in list_strategies()]
    logger.info("Active strategies: %s", [s.name for s in strategies])

    # Now that exchange + control + strategies exist, start Telegram
    # (only if enabled on this bot instance — typically only one mode's bot
    # has TELEGRAM_ENABLED=true to avoid 3 simultaneous Telegram pollers)
    if settings.telegram_enabled:
        telegram = TelegramNotifier(
            control=control, exchange=exchange, strategies=strategies, repo=repo,
        )
    else:
        telegram = TelegramNotifier(token="", chat_id="")  # disabled stub
    try:
        await telegram.start()
        if telegram.configured:
            from hypertrade.notify.telegram import MODE_BADGE
            badge = MODE_BADGE.get(settings.exchange_mode, settings.exchange_mode.upper())
            await telegram.send(f"{badge} 🚀 <b>HyperTrade started</b>")
    except Exception:
        logger.exception("Telegram notifier failed to start")

    # Set up WebSocket for real-time prices
    ws = HyperLiquidWebSocket()

    def on_price(symbol: str, price: float) -> None:
        if hasattr(exchange, "set_price"):
            exchange.set_price(symbol, price)

    ws.on_price(on_price)

    # Subscribe to candles for each active strategy
    for strat in strategies:
        ws.subscribe_candles(strat.symbol, strat.timeframe)

    # Start WebSocket in background
    ws_task = asyncio.create_task(ws.connect())
    logger.info("WebSocket feed started (real-time prices)")

    # Apply runtime leverage overrides + push leverage settings to exchange
    if control:
        overrides = await control.get_all_leverage_overrides()
        for s in strategies:
            if s.name in overrides:
                s.leverage = overrides[s.name]

    # Per-coin leverage = max across strategies trading that coin
    per_coin_leverage: dict[str, int] = {}
    for s in strategies:
        per_coin_leverage[s.symbol] = max(per_coin_leverage.get(s.symbol, 1), s.leverage)
    for coin, lev in per_coin_leverage.items():
        ok = await exchange.update_leverage(coin, lev, is_cross=True)
        logger.info(
            "Configured %s leverage=%dx (%s)",
            coin,
            lev,
            "ok" if ok else "FAILED",
        )

    # Start HTTP API server (with control + exchange refs for endpoints)
    api_runner = await start_api_server(
        port=settings.api_port,
        control=control,
        exchange=exchange,
        strategies=strategies,
        repo=repo,
    )

    # Create runner
    runner = EngineRunner(
        exchange=exchange,
        strategies=strategies,
        repo=repo,
        event_bus=event_bus,
        control=control,
    )

    # Restore strategy state from DB (positions open before restart)
    await runner.startup()

    # Run loop
    logger.info("Starting engine (poll interval: %ds)", settings.poll_interval_seconds)
    while not _shutdown.is_set():
        try:
            await runner.tick()
        except Exception:
            logger.exception("Engine tick failed")

        try:
            await asyncio.wait_for(
                _shutdown.wait(), timeout=settings.poll_interval_seconds
            )
        except asyncio.TimeoutError:
            pass

    # Cleanup
    logger.info("Shutting down...")
    await api_runner.cleanup()
    await ws.close()
    ws_task.cancel()
    if telegram:
        await telegram.stop()
    await event_bus.close()
    if control:
        await control.close()
    if repo:
        await repo.close()


def run() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)
    asyncio.run(main())


if __name__ == "__main__":
    run()
