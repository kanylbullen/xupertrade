"""Simulated exchange for paper trading.

Accounting model:
- Long position open: cash -= (price * size + fee). Position book value is
  tracked separately; equity = cash + position_market_value.
- Long position close: cash += (price * size - fee). PnL is the difference
  between close and open cash flows.
- Short position open: cash += (price * size - fee) (we receive proceeds).
  A liability equal to price * size is implied via the position.
- Short position close: cash -= (price * size + fee).
- Equity = cash + sum(market_value for longs) - sum(market_value for shorts)
        = cash + sum(entry_value + unrealized_pnl) for longs
        + sum(2 * entry_value - market_value) for shorts... simpler:
  Equity = cash + sum of (long position_market_value)
         - sum of (short position_market_value already credited to cash)
  The cleanest formulation: track position cost basis in cash via proceeds
  and deductions, then equity = cash + unrealized_pnl across all positions.

Implementation choice:
- For longs: cash -= entry_value at open, cash += exit_value at close.
  During open position, equity = cash + entry_value + unrealized_pnl
  = cash + position_size * current_price.
- For shorts: cash += entry_value at open (short proceeds).
  Short liability = entry_value. Equity = cash - entry_value + unrealized_pnl
  = cash - position_size * current_price.
"""

import uuid
from datetime import datetime, timezone

from hypertrade.exchange.base import (
    Balance,
    Exchange,
    Order,
    OrderStatus,
    OrderType,
    Position,
)


class PaperExchange(Exchange):
    """Simulated exchange for paper trading."""

    def __init__(self, initial_balance: float = 10_000.0):
        self._balance = initial_balance
        self._positions: dict[str, Position] = {}
        self._orders: list[Order] = []
        self._prices: dict[str, float] = {}

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price
        self._update_unrealized_pnl()

    def _update_unrealized_pnl(self) -> None:
        for symbol, pos in self._positions.items():
            if symbol in self._prices:
                current = self._prices[symbol]
                if pos.side == "long":
                    pos.unrealized_pnl = (current - pos.entry_price) * pos.size
                else:
                    pos.unrealized_pnl = (pos.entry_price - current) * pos.size

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
    ) -> Order:
        fill_price = price if price else self._prices.get(symbol, 0.0)
        if fill_price <= 0:
            return Order(
                id=str(uuid.uuid4()),
                symbol=symbol,
                side=side,
                size=size,
                order_type=order_type,
                price=price,
                status=OrderStatus.REJECTED,
            )

        order = Order(
            id=str(uuid.uuid4()),
            symbol=symbol,
            side=side,
            size=size,
            order_type=order_type,
            price=price,
            filled_price=fill_price,
            status=OrderStatus.FILLED,
            timestamp=datetime.now(timezone.utc),
        )
        self._orders.append(order)

        fee = fill_price * size * 0.00045  # HyperLiquid taker fee

        if side == "buy":
            self._handle_buy(symbol, size, fill_price, fee)
        else:
            self._handle_sell(symbol, size, fill_price, fee)

        return order

    def _handle_buy(self, symbol: str, size: float, price: float, fee: float) -> None:
        existing = self._positions.get(symbol)

        if existing and existing.side == "short":
            # Closing (possibly partial) short position: pay price * size_covered + fee
            size_covered = min(size, existing.size)
            self._balance -= (price * size_covered) + fee

            remaining_short = existing.size - size_covered
            if remaining_short > 0:
                existing.size = remaining_short
                return

            # Fully covered the short — remove position
            del self._positions[symbol]
            flip_size = size - existing.size  # any leftover opens a long
            if flip_size > 0:
                # Deduct cost of the new long leg
                self._balance -= price * flip_size
                self._positions[symbol] = Position(
                    symbol=symbol, side="long", size=flip_size, entry_price=price
                )
            return

        # Open or add to long — deduct notional and fee
        self._balance -= (price * size) + fee
        if existing:
            total_size = existing.size + size
            existing.entry_price = (
                (existing.entry_price * existing.size) + (price * size)
            ) / total_size
            existing.size = total_size
        else:
            self._positions[symbol] = Position(
                symbol=symbol, side="long", size=size, entry_price=price
            )

    def _handle_sell(self, symbol: str, size: float, price: float, fee: float) -> None:
        existing = self._positions.get(symbol)

        if existing and existing.side == "long":
            # Closing (possibly partial) long: receive price * size_closed - fee
            size_closed = min(size, existing.size)
            self._balance += (price * size_closed) - fee

            remaining_long = existing.size - size_closed
            if remaining_long > 0:
                existing.size = remaining_long
                return

            del self._positions[symbol]
            flip_size = size - existing.size  # any leftover opens a short
            if flip_size > 0:
                # Receive short proceeds for the new short leg
                self._balance += price * flip_size
                self._positions[symbol] = Position(
                    symbol=symbol, side="short", size=flip_size, entry_price=price
                )
            return

        # Open or add to short — receive proceeds minus fee
        self._balance += (price * size) - fee
        if existing:
            total_size = existing.size + size
            existing.entry_price = (
                (existing.entry_price * existing.size) + (price * size)
            ) / total_size
            existing.size = total_size
        else:
            self._positions[symbol] = Position(
                symbol=symbol, side="short", size=size, entry_price=price
            )

    async def cancel_order(self, order_id: str) -> bool:
        return False

    async def get_positions(self) -> list[Position]:
        self._update_unrealized_pnl()
        return list(self._positions.values())

    async def get_position(self, symbol: str) -> Position | None:
        self._update_unrealized_pnl()
        return self._positions.get(symbol)

    async def get_balance(self) -> Balance:
        self._update_unrealized_pnl()

        # Equity includes market value of open positions.
        # Longs: cash holds (initial - cost); market value of position = size * current_price.
        # Shorts: cash holds (initial + proceeds); liability = size * current_price.
        position_equity = 0.0
        total_unrealized = 0.0
        for pos in self._positions.values():
            current = self._prices.get(pos.symbol, pos.entry_price)
            total_unrealized += pos.unrealized_pnl
            if pos.side == "long":
                position_equity += pos.size * current
            else:
                # Short: we received entry_price*size at open; now owe current*size
                position_equity -= pos.size * current

        total = self._balance + position_equity
        # Available = cash minus margin for open longs (simple model: free cash)
        available = self._balance

        return Balance(
            total=total,
            available=available,
            unrealized_pnl=total_unrealized,
        )

    async def get_current_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)
