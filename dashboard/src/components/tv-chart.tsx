"use client";

import { useEffect, useRef } from "react";

const SYMBOL_MAP: Record<string, string> = {
  BTC: "BINANCE:BTCUSDT",
  ETH: "BINANCE:ETHUSDT",
  SOL: "BINANCE:SOLUSDT",
  XRP: "BINANCE:XRPUSDT",
  BNB: "BINANCE:BNBUSDT",
  DOGE: "BINANCE:DOGEUSDT",
  VVV: "COINBASE:VVVUSD",  // Venice — not on Binance
};

const INTERVAL_MAP: Record<string, string> = {
  "1m": "1",
  "5m": "5",
  "15m": "15",
  "30m": "30",
  "1h": "60",
  "4h": "240",
  "1d": "D",
};

// TradingView study IDs for each strategy's indicators
const STRATEGY_STUDIES: Record<string, string[]> = {
  supertrend: [
    "STD;Supertrend",         // ST overlay (note: TV's default is non-adaptive)
    "STD;EMA",                // 50 EMA — trend filter
    "STD;DMI",                // ADX/DI ± — regime detection input
    "STD;Average_True_Range", // ATR — drives the adaptive multiplier
  ],
  rsi_momentum: [
    "STD;RSI",                // RSI in separate pane
  ],
  bb_short: [
    "STD;Bollinger_Bands",    // BB(20, 2) overlay — entry triggers off the upper band
  ],
  sma_rsi: [
    "STD;SMA",                // 50 SMA
    "STD;SMA",                // 200 SMA (appears twice, TV handles it)
    "STD;RSI",
  ],
  volatility_breakout: [
    "STD;Keltner_Channels",   // KC = EMA(22) ± ATR(10)×2 — entry trigger
    "STD;EMA",                // 220 EMA — trend filter
    "STD;DMI",                // ADX/DI ± — trend strength filter
    "STD;RSI",                // RSI(14) — momentum filter
  ],
  btc_mean_reversion: [
    "STD;EMA",                // EMA(200) — mean baseline + entry filter
    "STD;RSI",                // RSI(14) — primary signal at 20/65
    "STD;Stochastic",         // Stoch %K(14) — confirmation at 25/75
  ],
};

export function TradingViewChart({
  symbol,
  timeframe = "4h",
  height = 500,
  strategy,
}: {
  symbol: string;
  timeframe?: string;
  height?: number;
  strategy?: string;
}) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    const tvSymbol = SYMBOL_MAP[symbol] ?? `BINANCE:${symbol}USDT`;
    const interval = INTERVAL_MAP[timeframe] ?? "240";
    const studies = strategy ? STRATEGY_STUDIES[strategy] ?? [] : [];

    containerRef.current.innerHTML = "";

    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.type = "text/javascript";
    script.async = true;
    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol: tvSymbol,
      interval: interval,
      timezone: "Europe/Stockholm",
      theme: "dark",
      style: "1",
      locale: "en",
      hide_top_toolbar: false,
      hide_legend: false,
      allow_symbol_change: true,
      save_image: false,
      calendar: false,
      studies: studies,
      support_host: "https://www.tradingview.com",
    });

    const wrapper = document.createElement("div");
    wrapper.className = "tradingview-widget-container";
    wrapper.style.height = `${height}px`;
    wrapper.style.width = "100%";

    const innerDiv = document.createElement("div");
    innerDiv.className = "tradingview-widget-container__widget";
    innerDiv.style.height = "calc(100% - 32px)";
    innerDiv.style.width = "100%";

    wrapper.appendChild(innerDiv);
    wrapper.appendChild(script);
    containerRef.current.appendChild(wrapper);
  }, [symbol, timeframe, height, strategy]);

  return <div ref={containerRef} style={{ height: `${height}px` }} />;
}

export function TradingViewTicker({ symbols }: { symbols: string[] }) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    containerRef.current.innerHTML = "";

    const tvSymbols = symbols.map((s) => ({
      proName: SYMBOL_MAP[s] ?? `BINANCE:${s}USDT`,
      title: `${s}/USDT`,
    }));

    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js";
    script.type = "text/javascript";
    script.async = true;
    script.innerHTML = JSON.stringify({
      symbols: tvSymbols,
      showSymbolLogo: true,
      isTransparent: true,
      displayMode: "adaptive",
      colorTheme: "dark",
      locale: "en",
    });

    const wrapper = document.createElement("div");
    wrapper.className = "tradingview-widget-container";

    const innerDiv = document.createElement("div");
    innerDiv.className = "tradingview-widget-container__widget";

    wrapper.appendChild(innerDiv);
    wrapper.appendChild(script);
    containerRef.current.appendChild(wrapper);
  }, [symbols]);

  return <div ref={containerRef} />;
}
