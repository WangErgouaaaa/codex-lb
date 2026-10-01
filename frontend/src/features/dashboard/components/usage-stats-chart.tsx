import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "@/components/lazy-recharts";
import { ChartTooltip } from "@/features/reports/components/chart-tooltip";
import { formatCompactNumber } from "@/utils/formatters";
import { MAX_VISIBLE_SERIES, OTHER_SERIES_KEY } from "./usage-stats-constants";

const SERIES_COLORS: readonly string[] = [
  "#3b82f6",
  "#10b981",
  "#f59e0b",
  "#ec4899",
  "#8b5cf6",
  "#06b6d4",
  "#84cc16",
  "#f97316",
  // Fallbacks once MAX_VISIBLE_SERIES exceeds the palette above.
  "#6366f1",
  "#14b8a6",
].slice(0, MAX_VISIBLE_SERIES);

const OTHER_SERIES_COLOR = "#94a3b8";

export type UsageStatsChartProps = {
  rows: Array<Record<string, number | string>>;
  models: string[];
  hasOther: boolean;
  names: Record<string, string>;
};

export function UsageStatsChart({ rows, models, hasOther, names }: UsageStatsChartProps) {
  const legendFormatter = (value: unknown) => names[String(value)] ?? String(value);

  return (
    <div className="h-[260px]" data-testid="usage-stats-chart">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 5, right: 10, left: 10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
            axisLine={false}
            tickLine={false}
            interval="preserveStartEnd"
            minTickGap={14}
          />
          <YAxis
            tick={{ fontSize: 10, fill: "var(--muted-foreground)" }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(value: number) => formatCompactNumber(value)}
            width={46}
          />
          <Tooltip
            cursor={{ fill: "hsl(var(--muted))", fillOpacity: 0.4 }}
            content={
              <ChartTooltip
                names={names}
                formatValue={(value: number) => formatCompactNumber(value)}
              />
            }
          />
          <Legend
            iconType="circle"
            iconSize={8}
            wrapperStyle={{ fontSize: 11, paddingTop: 6 }}
            formatter={legendFormatter}
          />
          {models.map((model, index) => (
            <Bar
              key={model}
              dataKey={model}
              stackId="usage"
              fill={SERIES_COLORS[index % SERIES_COLORS.length]}
            />
          ))}
          {hasOther ? (
            <Bar dataKey={OTHER_SERIES_KEY} stackId="usage" fill={OTHER_SERIES_COLOR} />
          ) : null}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
