import { lazy, Suspense, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { AlertMessage } from "@/components/alert-message";
import { Button } from "@/components/ui/button";
import { SpinnerBlock } from "@/components/ui/spinner";
import { getBrowserReportsTimeZone } from "@/features/reports/date";
import { useUsageStats } from "@/features/dashboard/hooks/use-usage-stats";
import {
  DEFAULT_USAGE_STATS_RANGE,
  type UsageStatsRange,
} from "@/features/dashboard/schemas";
import { formatCompactNumber, formatNumber } from "@/utils/formatters";
import { MAX_VISIBLE_SERIES, OTHER_SERIES_KEY } from "./usage-stats-constants";

const UsageStatsChart = lazy(() =>
  import("./usage-stats-chart").then((module) => ({
    default: (props: UsageStatsChartProps) => <module.UsageStatsChart {...props} />,
  })),
);
type UsageStatsChartProps = {
  rows: Array<Record<string, number | string>>;
  models: string[];
  hasOther: boolean;
  names: Record<string, string>;
};

const USAGE_RANGE_OPTIONS: ReadonlyArray<{ value: UsageStatsRange; labelKey: string }> = [
  { value: "today", labelKey: "dashboard.usageStats.range.today" },
  { value: "7d", labelKey: "dashboard.usageStats.range.7d" },
  { value: "30d", labelKey: "dashboard.usageStats.range.30d" },
];

export function UsageStatsPanel() {
  const { t } = useTranslation();
  const [range, setRange] = useState<UsageStatsRange>(DEFAULT_USAGE_STATS_RANGE);
  const [timeZone] = useState(() => getBrowserReportsTimeZone());
  const statsQuery = useUsageStats(range, timeZone);
  const data = statsQuery.data;

  const chartData = useMemo(() => {
    if (!data) {
      return { rows: [], models: [], hasOther: false };
    }
    const totals = new Map<string, number>();
    for (const bucket of data.series) {
      for (const [model, value] of Object.entries(bucket.values)) {
        totals.set(model, (totals.get(model) ?? 0) + value);
      }
    }
    const rankedModels = [...totals.entries()]
      .sort((left, right) => right[1] - left[1])
      .map(([model]) => model);
    const visibleModels = rankedModels.slice(0, MAX_VISIBLE_SERIES);
    const hasOther = rankedModels.length > visibleModels.length;
    const rows = data.series.map((bucket) => {
      const row: Record<string, number | string> = { label: bucket.label };
      let otherTotal = 0;
      for (const [model, value] of Object.entries(bucket.values)) {
        if (visibleModels.includes(model)) {
          row[model] = value;
        } else {
          otherTotal += value;
        }
      }
      if (hasOther) {
        row[OTHER_SERIES_KEY] = otherTotal;
      }
      return row;
    });
    return { rows, models: visibleModels, hasOther };
  }, [data]);

  const seriesNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const entry of data?.byModel ?? []) {
      names[entry.model] = entry.model;
    }
    names[OTHER_SERIES_KEY] = t("dashboard.usageStats.other");
    return names;
  }, [data, t]);

  const isEmpty = !data || data.series.every((bucket) => Object.keys(bucket.values).length === 0);

  const cachedShare =
    data && data.summary.totalTokens > 0
      ? Math.round((data.summary.totalCachedTokens / data.summary.totalTokens) * 100)
      : null;

  return (
    <div className="rounded-xl border bg-card p-5" data-testid="usage-stats-panel">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-foreground">{t("dashboard.usageStats.title")}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">{t("dashboard.usageStats.subtitle")}</div>
        </div>
        <div className="flex items-center gap-1.5" role="group" aria-label={t("dashboard.usageStats.title")}>
          {USAGE_RANGE_OPTIONS.map((option) => {
            const isSelected = range === option.value;
            return (
              <Button
                key={option.value}
                type="button"
                variant={isSelected ? "default" : "outline"}
                size="sm"
                aria-pressed={isSelected}
                onClick={() => setRange(option.value)}
              >
                {t(option.labelKey)}
              </Button>
            );
          })}
        </div>
      </div>

      {statsQuery.isPending && !data ? (
        <div className="py-8">
          <SpinnerBlock />
        </div>
      ) : statsQuery.error && !data ? (
        <div className="mt-4 space-y-3">
          <AlertMessage variant="error">
            {t("dashboard.usageStats.errors.load", {
              error: statsQuery.error instanceof Error ? statsQuery.error.message : String(statsQuery.error),
            })}
          </AlertMessage>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={statsQuery.isFetching}
            onClick={() => {
              void statsQuery.refetch();
            }}
          >
            {t("common.actions.retry")}
          </Button>
        </div>
      ) : data ? (
        <>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard
              label={t("dashboard.usageStats.stat.totalTokens")}
              value={formatCompactNumber(data.summary.totalTokens)}
            />
            <StatCard
              label={t("dashboard.usageStats.stat.requests")}
              value={formatNumber(data.summary.totalRequests)}
              sub={
                data.summary.totalErrors > 0
                  ? t("dashboard.usageStats.stat.failedRequests", { count: data.summary.totalErrors })
                  : undefined
              }
            />
            <StatCard
              label={t("dashboard.usageStats.stat.cachedTokens")}
              value={formatCompactNumber(data.summary.totalCachedTokens)}
              sub={cachedShare != null ? t("dashboard.usageStats.stat.shareOfTotal", { percent: cachedShare }) : undefined}
            />
            {data.bucket === "day" ? (
              <StatCard
                label={t("dashboard.usageStats.stat.avgPerDay")}
                value={formatCompactNumber(data.summary.avgTokensPerDay)}
              />
            ) : (
              <StatCard
                label={t("dashboard.usageStats.stat.models")}
                value={formatNumber(data.summary.modelCount)}
              />
            )}
          </div>

          {isEmpty ? (
            <div className="mt-4 flex h-[220px] items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
              {t("dashboard.usageStats.empty")}
            </div>
          ) : (
            <>
              <div className="mt-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-medium text-muted-foreground">
                    {t("dashboard.usageStats.chart.title")}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {t("dashboard.usageStats.chart.total")}:{" "}
                    <span className="font-semibold text-foreground">
                      {formatCompactNumber(data.summary.totalTokens)}
                    </span>
                  </div>
                </div>
                <div className="mt-2">
                  <Suspense fallback={<div className="h-[260px] rounded-lg bg-muted/30" />}>
                    <UsageStatsChart
                      rows={chartData.rows}
                      models={chartData.models}
                      hasOther={chartData.hasOther}
                      names={seriesNames}
                    />
                  </Suspense>
                </div>
              </div>

              <div
                data-testid="usage-stats-table-scroll"
                className="mt-4 max-h-[17.5rem] overflow-x-auto overflow-y-auto"
              >
                <table className="w-full table-fixed text-xs min-w-[680px]">
                  <colgroup>
                    <col style={{ width: "28%" }} />
                    <col style={{ width: "10%" }} />
                    <col style={{ width: "13%" }} />
                    <col style={{ width: "13%" }} />
                    <col style={{ width: "13%" }} />
                    <col style={{ width: "13%" }} />
                    <col style={{ width: "10%" }} />
                  </colgroup>
                  <thead className="sticky top-0 z-10 bg-card">
                    <tr className="border-b text-left text-muted-foreground">
                      <th className="pb-2 pr-4 font-medium">{t("dashboard.usageStats.table.model")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.requests")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.input")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.output")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.cached")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.total")}</th>
                      <th className="pb-2 text-right font-medium">{t("dashboard.usageStats.table.share")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.byModel.map((entry) => (
                      <tr
                        key={entry.model}
                        data-testid={`usage-stats-row-${entry.model}`}
                        className="border-b border-border/50 last:border-0"
                      >
                        <td className="py-2.5 pr-4 font-medium text-foreground">
                          <span className="break-all">{seriesNames[entry.model] ?? entry.model}</span>
                        </td>
                        <td className="py-2.5 pr-4 text-right text-foreground">{formatNumber(entry.requests)}</td>
                        <td className="py-2.5 pr-4 text-right text-foreground">{formatCompactNumber(entry.inputTokens)}</td>
                        <td className="py-2.5 pr-4 text-right text-foreground">{formatCompactNumber(entry.outputTokens)}</td>
                        <td className="py-2.5 pr-4 text-right text-muted-foreground">{formatCompactNumber(entry.cachedInputTokens)}</td>
                        <td className="py-2.5 pr-4 text-right font-medium text-foreground">{formatCompactNumber(entry.totalTokens)}</td>
                        <td className="py-2.5 text-right text-muted-foreground">{entry.percentage.toFixed(1)}%</td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot className="sticky bottom-0 bg-card">
                    <tr className="border-t text-muted-foreground">
                      <td className="py-2.5 pr-4 font-medium text-foreground">{t("dashboard.usageStats.table.totalRow")}</td>
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">{formatNumber(data.summary.totalRequests)}</td>
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">{formatCompactNumber(data.summary.totalInputTokens)}</td>
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">{formatCompactNumber(data.summary.totalOutputTokens)}</td>
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">{formatCompactNumber(data.summary.totalCachedTokens)}</td>
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">{formatCompactNumber(data.summary.totalTokens)}</td>
                      <td className="py-2.5 text-right font-medium text-foreground">100%</td>
                    </tr>
                  </tfoot>
                </table>
              </div>
            </>
          )}
        </>
      ) : null}
    </div>
  );
}

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-lg bg-muted/50 px-3 py-2.5" data-testid="usage-stats-stat-card">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate text-base font-semibold tabular-nums text-foreground">{value}</div>
      {sub ? <div className="text-[11px] text-muted-foreground">{sub}</div> : null}
    </div>
  );
}
