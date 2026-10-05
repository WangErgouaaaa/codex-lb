import { useTranslation } from "react-i18next";

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "@/components/lazy-recharts";
import { formatCompactNumber } from "@/utils/formatters";

export const WINDOW_PRIMARY_KEY = "primaryPercent";
export const WINDOW_SECONDARY_KEY = "secondaryPercent";
const PRIMARY_COLOR = "#3b82f6";
const SECONDARY_COLOR = "#10b981";

export type UsageWindowRow = {
  label: string;
  primaryPercent: number;
  secondaryPercent: number;
  primaryCredits: number;
  secondaryCredits: number;
};

export type UsageStatsWindowChartProps = {
  /** One row per bucket; percentages already share-of-range (0-100). */
  rows: UsageWindowRow[];
};

export function UsageStatsWindowChart({ rows }: UsageStatsWindowChartProps) {
  const { t } = useTranslation();
  const names: Record<string, string> = {
    [WINDOW_PRIMARY_KEY]: t("dashboard.usage.fiveHourCredits"),
    [WINDOW_SECONDARY_KEY]: t("dashboard.usage.weeklyCredits"),
  };

  return (
    <div className="h-[260px]" data-testid="usage-stats-window-chart">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 5, right: 10, left: 10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
          <XAxis
            dataKey="label"
            tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
            axisLine={false}
            tickLine={false}
            interval="preserveStartEnd"
            minTickGap={16}
          />
          <YAxis
            tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(value: number) => `${value.toFixed(0)}%`}
            width={52}
          />
          <Tooltip content={<WindowChartTooltip names={names} />} />
          <Legend
            iconType="circle"
            iconSize={9}
            wrapperStyle={{ fontSize: 12, paddingTop: 6 }}
            formatter={(value: unknown) => names[String(value)] ?? String(value)}
          />
          <Line
            dataKey={WINDOW_PRIMARY_KEY}
            stroke={PRIMARY_COLOR}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 3 }}
            type="monotone"
          />
          <Line
            dataKey={WINDOW_SECONDARY_KEY}
            stroke={SECONDARY_COLOR}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 3 }}
            type="monotone"
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

type TooltipEntry = {
  dataKey?: string | number;
  color?: string;
  value?: number | string;
  payload?: UsageWindowRow;
};

function WindowChartTooltip({
  active,
  payload,
  label,
  names,
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string | number;
  names: Record<string, string>;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-xl border border-border bg-popover p-2.5 shadow-md">
      {label != null ? (
        <p className="mb-1.5 px-2 text-[11px] font-medium text-muted-foreground">{label}</p>
      ) : null}
      <div className="space-y-1">
        {payload.map((entry, index) => {
          const key = String(entry.dataKey ?? index);
          const percent = typeof entry.value === "number" ? entry.value : 0;
          const credits =
            entry.payload?.[key === WINDOW_PRIMARY_KEY ? "primaryCredits" : "secondaryCredits"] ?? 0;
          return (
            <div
              key={key}
              className="flex items-center gap-2 rounded-lg px-2.5 py-1.5"
              style={{ backgroundColor: `${entry.color ?? "#888"}18` }}
            >
              <span
                className="h-2 w-2 shrink-0 rounded-full"
                style={{ backgroundColor: entry.color ?? "#888" }}
              />
              <span className="text-xs text-popover-foreground">{names[key] ?? key}</span>
              <span className="ml-auto pl-2 text-xs font-semibold text-popover-foreground">
                {percent.toFixed(1)}% · {formatCompactNumber(credits)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
