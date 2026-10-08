import { useTranslation } from "react-i18next";

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
import { formatCompactNumber } from "@/utils/formatters";
import { USAGE_CHART_COLORS } from "./usage-stats-constants";

export type UsageAccountChartRow = {
  name: string;
  tokens: number;
  /** Credits spent as a share of the account's weekly capacity (0-100). */
  quotaPercent: number;
  capacityCredits: number;
};

export type UsageStatsAccountsChartProps = {
  /** Sorted by totalTokens descending (busiest first). */
  rows: UsageAccountChartRow[];
};

export function UsageStatsAccountsChart({ rows }: UsageStatsAccountsChartProps) {
  const height = Math.min(320, Math.max(200, 36 * rows.length));
  const displayRows = rows;

  return (
    <div style={{ height }} data-testid="usage-stats-accounts-chart">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={displayRows} layout="vertical" margin={{ top: 5, right: 10, left: 10, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" horizontal={false} />
          <XAxis
            type="number"
            tick={{ fontSize: 12, fill: "var(--muted-foreground)" }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(value: number) => formatCompactNumber(value)}
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
            content={<AccountChartTooltip />}
          />
          <Bar dataKey="tokens" radius={[0, 4, 4, 0]}>
            {displayRows.map((row, index) => (
              <Cell key={row.name} fill={USAGE_CHART_COLORS[index % USAGE_CHART_COLORS.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

type TooltipEntry = {
  payload?: UsageAccountChartRow;
};

function AccountChartTooltip({ active, payload }: { active?: boolean; payload?: TooltipEntry[] }) {
  const { t } = useTranslation();
  if (!active || !payload?.length) return null;
  const row = payload[0]?.payload;
  if (!row) return null;
  return (
    <div className="rounded-xl border border-border bg-popover p-2.5 shadow-md">
      <p className="px-2 text-xs font-semibold text-popover-foreground">
        {t("dashboard.usageStats.tooltip.accountQuota", {
          name: row.name,
          tokens: formatCompactNumber(row.tokens),
          percent: row.capacityCredits > 0 ? `${row.quotaPercent.toFixed(1)}%` : "—",
        })}
      </p>
    </div>
  );
}
