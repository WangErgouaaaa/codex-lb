import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "@/components/lazy-recharts";
import { ChartTooltip } from "@/features/reports/components/chart-tooltip";
import { USAGE_CHART_COLORS } from "./usage-stats-constants";

export type UsageUnitPriceRow = {
  model: string;
  name: string;
  /** Credits per 1M attributed tokens. */
  ratio: number;
};

export type UsageStatsUnitPriceChartProps = {
  /** Sorted ascending by ratio (cheapest first). */
  rows: UsageUnitPriceRow[];
};

export function UsageStatsUnitPriceChart({ rows }: UsageStatsUnitPriceChartProps) {
  const height = Math.min(320, Math.max(200, 36 * rows.length));
  // rows arrive sorted ascending by ratio (cheapest first). A recharts
  // vertical BarChart lists the first data entry at the bottom of the
  // category axis, so reverse to put the cheapest model visually on top.
  const displayRows = [...rows].reverse();

  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={displayRows} layout="vertical" margin={{ top: 5, right: 10, left: 10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" horizontal={false} />
          <XAxis
            type="number"
            tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(value: number) => value.toFixed(1)}
          />
          <YAxis
            type="category"
            dataKey="name"
            tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
            axisLine={false}
            tickLine={false}
            width={150}
          />
          <Tooltip
            cursor={{ fill: "hsl(var(--muted))", fillOpacity: 0.4 }}
            content={<ChartTooltip formatValue={(value: number) => value.toFixed(1)} />}
          />
          <Bar dataKey="ratio" radius={[0, 4, 4, 0]}>
            {displayRows.map((row, index) => (
              <Cell key={row.model} fill={USAGE_CHART_COLORS[index % USAGE_CHART_COLORS.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
