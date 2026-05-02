"use client";

import { formatDate, formatDateTime } from "@/lib/format";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

type DataPoint = {
  timestamp: string;
  totalEquity: number;
};

export function EquityChart({ data }: { data: DataPoint[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-[300px] items-center justify-center text-muted-foreground">
        No equity data yet. Start the bot to begin tracking.
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={300}>
      <LineChart data={data}>
        <CartesianGrid strokeDasharray="3 3" stroke="#333" />
        <XAxis
          dataKey="timestamp"
          tick={{ fontSize: 12 }}
          tickFormatter={(v) => formatDate(v)}
        />
        <YAxis
          tick={{ fontSize: 12 }}
          tickFormatter={(v) => `$${v.toLocaleString()}`}
        />
        <Tooltip
          formatter={(value) => [`$${Number(value).toLocaleString()}`, "Equity"]}
          labelFormatter={(label) => formatDateTime(label)}
        />
        <Line
          type="monotone"
          dataKey="totalEquity"
          stroke="#22c55e"
          strokeWidth={2}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
