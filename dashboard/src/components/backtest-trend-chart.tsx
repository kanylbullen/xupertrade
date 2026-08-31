"use client";

import { formatDate, formatDateTime } from "@/lib/format";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

/**
 * APR + Sharpe over run date for ONE strategy — the "did that parameter
 * change actually help" view for /backtests. Mirrors `EquityChart`
 * (recharts, fixed height, muted empty state).
 *
 * Dual Y-axis: APR is a percentage, Sharpe an unbounded ratio, so one
 * shared scale would flatten one of them. Green APR matches the equity
 * curve's positive color; Sharpe gets the secondary blue.
 */
type TrendPoint = {
  createdAt: string; // ISO timestamp of the run
  apr: number;
  sharpe: number;
};

export function BacktestTrendChart({ data }: { data: TrendPoint[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-[300px] items-center justify-center text-muted-foreground">
        No saved runs for this strategy yet.
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={300}>
      <LineChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#333" />
        <XAxis
          dataKey="createdAt"
          tick={{ fontSize: 12 }}
          tickFormatter={(v) => formatDate(v)}
        />
        <YAxis
          yAxisId="apr"
          tick={{ fontSize: 12 }}
          tickFormatter={(v) => `${v}%`}
        />
        <YAxis yAxisId="sharpe" orientation="right" tick={{ fontSize: 12 }} />
        <Tooltip
          formatter={(value, name) =>
            name === "APR"
              ? [`${Number(value).toFixed(2)}%`, "APR"]
              : [Number(value).toFixed(2), "Sharpe"]
          }
          labelFormatter={(label) => formatDateTime(label)}
        />
        <Legend />
        <Line
          yAxisId="apr"
          type="monotone"
          dataKey="apr"
          name="APR"
          stroke="#22c55e"
          strokeWidth={2}
          dot={{ r: 3 }}
        />
        <Line
          yAxisId="sharpe"
          type="monotone"
          dataKey="sharpe"
          name="Sharpe"
          stroke="#3b82f6"
          strokeWidth={2}
          dot={{ r: 3 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
