import { lazy, Suspense, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { AlertMessage } from "@/components/alert-message";
import { Button } from "@/components/ui/button";
import { SpinnerBlock } from "@/components/ui/spinner";
import { getBrowserReportsTimeZone } from "@/features/reports/date";
import { useUsageStats } from "@/features/dashboard/hooks/use-usage-stats";
import {
  DEFAULT_USAGE_STATS_METRIC,
  DEFAULT_USAGE_STATS_RANGE,
  type UsageStatsMetric,
  type UsageStatsRange,
} from "@/features/dashboard/schemas";
import { formatCompactNumber, formatCurrency, formatNumber } from "@/utils/formatters";
import {
  isUsageRatioDisplayable,
  MAX_VISIBLE_SERIES,
  OTHER_SERIES_KEY,
  usageUnitPriceRatio,
} from "./usage-stats-constants";
import type { UsageUnitPriceRow } from "./usage-stats-unitprice-chart";

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
  formatValue?: (value: number) => string;
};

const UsageStatsUnitPriceChart = lazy(() =>
  import("./usage-stats-unitprice-chart").then((module) => ({
    default: (props: UsageStatsUnitPriceChartProps) => <module.UsageStatsUnitPriceChart {...props} />,
  })),
);
type UsageStatsUnitPriceChartProps = {
  rows: UsageUnitPriceRow[];
};

const UsageStatsWindowChart = lazy(() =>
  import("./usage-stats-window-chart").then((module) => ({
    default: (props: UsageStatsWindowChartProps) => <module.UsageStatsWindowChart {...props} />,
  })),
);
type UsageStatsWindowChartProps = {
  rows: import("./usage-stats-window-chart").UsageWindowRow[];
};

/** Frontend-only view; "unitprice" is served by the credits metric. */
type UsageStatsView = UsageStatsMetric | "unitprice";

function toApiMetric(view: UsageStatsView): UsageStatsMetric {
  return view === "unitprice" ? "credits" : view;
}

const USAGE_RANGE_OPTIONS: ReadonlyArray<{ value: UsageStatsRange; labelKey: string }> = [
  { value: "today", labelKey: "dashboard.usageStats.range.today" },
  { value: "7d", labelKey: "dashboard.usageStats.range.7d" },
  { value: "30d", labelKey: "dashboard.usageStats.range.30d" },
];

const USAGE_METRIC_OPTIONS: ReadonlyArray<{ value: UsageStatsView; labelKey: string }> = [
  { value: "tokens", labelKey: "dashboard.usageStats.metric.tokens" },
  { value: "credits", labelKey: "dashboard.usageStats.metric.credits" },
  { value: "cost", labelKey: "dashboard.usageStats.metric.cost" },
  { value: "unitprice", labelKey: "dashboard.usageStats.metric.unitprice" },
  { value: "window_credits", labelKey: "dashboard.usageStats.metric.window_credits" },
];

export function UsageStatsPanel() {
  const { t } = useTranslation();
  const [range, setRange] = useState<UsageStatsRange>(DEFAULT_USAGE_STATS_RANGE);
  const [view, setView] = useState<UsageStatsView>(DEFAULT_USAGE_STATS_METRIC);
  const [timeZone] = useState(() => getBrowserReportsTimeZone());
  const statsQuery = useUsageStats(range, toApiMetric(view), timeZone);
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

  const metricValueFormatter = (value: number): string => {
    if (view === "cost") return formatCurrency(value);
    if (view === "credits") return formatCompactNumber(value);
    return formatCompactNumber(value);
  };

  // Real unit price rows (credits per 1M attributed tokens), guarded by the
  // minimum sample size and sorted ascending so the cheapest model wins.
  const unitPriceRows = useMemo<Array<UsageUnitPriceRow>>(() => {
    if (!data) {
      return [];
    }
    return data.byModel
      .filter((entry) => isUsageRatioDisplayable(entry))
      .map((entry) => ({
        model: entry.model,
        name: seriesNames[entry.model] ?? entry.model,
        ratio: usageUnitPriceRatio(entry),
      }))
      .sort((left, right) => left.ratio - right.ratio);
  }, [data, seriesNames]);

  // Window view rows: each bucket's 5h/weekly attributed credits as a share
  // of that window's range total, so both lines normalize to 100%.
  const windowChartData = useMemo(() => {
    const buckets = data?.windowSeries ?? [];
    const primaryTotal = buckets.reduce((sum, bucket) => sum + bucket.primaryCredits, 0);
    const secondaryTotal = buckets.reduce((sum, bucket) => sum + bucket.secondaryCredits, 0);
    const toPercent = (value: number, total: number) => (total > 0 ? (value / total) * 100 : 0);
    return {
      rows: buckets.map((bucket) => ({
        label: bucket.label,
        primaryPercent: toPercent(bucket.primaryCredits, primaryTotal),
        secondaryPercent: toPercent(bucket.secondaryCredits, secondaryTotal),
        primaryCredits: bucket.primaryCredits,
        secondaryCredits: bucket.secondaryCredits,
      })),
      primaryTotal,
      secondaryTotal,
    };
  }, [data]);

  // The unitprice view locally re-sorts the table: guarded models ascending
  // by ratio first, unguarded models afterwards in server order.
  const tableRows = useMemo(() => {
    if (!data) {
      return [];
    }
    if (view !== "unitprice") {
      return data.byModel;
    }
    const guarded = data.byModel
      .filter((entry) => isUsageRatioDisplayable(entry))
      .sort((left, right) => usageUnitPriceRatio(left) - usageUnitPriceRatio(right));
    const unguarded = data.byModel.filter((entry) => !isUsageRatioDisplayable(entry));
    return [...guarded, ...unguarded];
  }, [data, view]);

  const isEmpty =
    !data ||
    (view === "window_credits"
      ? windowChartData.primaryTotal <= 0 && windowChartData.secondaryTotal <= 0
      : data.series.every((bucket) => Object.keys(bucket.values).length === 0));

  const cachedShare =
    data && data.summary.totalTokens > 0
      ? Math.round((data.summary.totalCachedTokens / data.summary.totalTokens) * 100)
      : null;
  const attributedShare =
    data && data.summary.totalRequests > 0
      ? Math.round((data.summary.attributedRequests / data.summary.totalRequests) * 100)
      : null;

  const cacheHitRate = (cached: number, input: number): number | null =>
    input > 0 ? Math.min(100, Math.round((cached / input) * 100)) : null;

  return (
    <div className="rounded-xl border bg-card p-5" data-testid="usage-stats-panel">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="text-sm font-semibold text-foreground">{t("dashboard.usageStats.title")}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">{t("dashboard.usageStats.subtitle")}</div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <div
            className="flex items-center gap-1.5"
            role="group"
            aria-label={t("dashboard.usageStats.metric.groupLabel")}
            data-testid="usage-stats-metric-toggle"
          >
            {USAGE_METRIC_OPTIONS.map((option) => {
              const isSelected = view === option.value;
              return (
                <Button
                  key={option.value}
                  type="button"
                  variant={isSelected ? "secondary" : "ghost"}
                  size="sm"
                  className="h-7 px-2.5 text-xs"
                  aria-pressed={isSelected}
                  onClick={() => setView(option.value)}
                >
                  {t(option.labelKey)}
                </Button>
              );
            })}
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
            {view === "tokens" ? (
              <>
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
              </>
            ) : view === "credits" ? (
              <>
                <StatCard
                  label={t("dashboard.usageStats.stat.totalCredits")}
                  value={formatCompactNumber(data.summary.totalCredits)}
                  sub={t("dashboard.usageStats.stat.creditsNote")}
                />
                <StatCard
                  label={t("dashboard.usageStats.stat.attributedRequests")}
                  value={formatNumber(data.summary.attributedRequests)}
                  sub={
                    attributedShare != null
                      ? t("dashboard.usageStats.stat.shareOfRequests", { percent: attributedShare })
                      : undefined
                  }
                />
                <StatCard
                  label={t("dashboard.usageStats.stat.requests")}
                  value={formatNumber(data.summary.totalRequests)}
                />
                {data.bucket === "day" ? (
                  <StatCard
                    label={t("dashboard.usageStats.stat.avgCreditsPerDay")}
                    value={formatCompactNumber(data.summary.totalCredits / Math.max(1, data.series.length))}
                  />
                ) : (
                  <StatCard
                    label={t("dashboard.usageStats.stat.models")}
                    value={formatNumber(data.summary.modelCount)}
                  />
                )}
              </>
            ) : view === "unitprice" ? (
              <>
                <StatCard
                  label={t("dashboard.usageStats.stat.totalCredits")}
                  value={formatCompactNumber(data.summary.totalCredits)}
                  sub={t("dashboard.usageStats.stat.creditsNote")}
                />
                <StatCard
                  label={t("dashboard.usageStats.stat.unitPrice")}
                  value={
                    data.summary.totalAttributedTokens > 0
                      ? (
                          (data.summary.totalCredits / data.summary.totalAttributedTokens) *
                          1_000_000
                        ).toFixed(1)
                      : "—"
                  }
                  sub={t("dashboard.usageStats.stat.unitPriceNote")}
                />
                <StatCard
                  label={t("dashboard.usageStats.stat.creditsPerRequest")}
                  value={(data.summary.totalCredits / Math.max(1, data.summary.attributedRequests)).toFixed(1)}
                  sub={t("dashboard.usageStats.stat.creditsPerRequestNote")}
                />
                <StatCard
                  label={t("dashboard.usageStats.stat.attributedRequests")}
                  value={formatNumber(data.summary.attributedRequests)}
                  sub={
                    attributedShare != null
                      ? t("dashboard.usageStats.stat.shareOfRequests", { percent: attributedShare })
                      : undefined
                  }
                />
              </>
            ) : view === "window_credits" ? (
              <>
                <StatCard
                  label={t("dashboard.usage.fiveHourCredits")}
                  value={formatCompactNumber(windowChartData.primaryTotal)}
                  sub={t("dashboard.usageStats.stat.creditsNote")}
                />
                <StatCard
                  label={t("dashboard.usage.weeklyCredits")}
                  value={formatCompactNumber(windowChartData.secondaryTotal)}
                  sub={t("dashboard.usageStats.stat.creditsNote")}
                />
                <StatCard
                  label={t("dashboard.usageStats.stat.attributedRequests")}
                  value={formatNumber(data.summary.attributedRequests)}
                  sub={
                    attributedShare != null
                      ? t("dashboard.usageStats.stat.shareOfRequests", { percent: attributedShare })
                      : undefined
                  }
                />
                {data.bucket === "day" ? (
                  <StatCard
                    label={t("dashboard.usageStats.stat.avgCreditsPerDay")}
                    value={formatCompactNumber(
                      windowChartData.secondaryTotal / Math.max(1, windowChartData.rows.length),
                    )}
                  />
                ) : (
                  <StatCard
                    label={t("dashboard.usageStats.stat.models")}
                    value={formatNumber(data.summary.modelCount)}
                  />
                )}
              </>
            ) : (
              <>
                <StatCard
                  label={t("dashboard.usageStats.stat.totalCost")}
                  value={formatCurrency(data.summary.totalCostUsd)}
                  sub={t("dashboard.usageStats.stat.costNote")}
                />
                <StatCard
                  label={t("dashboard.usageStats.stat.requests")}
                  value={formatNumber(data.summary.totalRequests)}
                />
                <StatCard
                  label={t("dashboard.usageStats.stat.cachedTokens")}
                  value={formatCompactNumber(data.summary.totalCachedTokens)}
                />
                {data.bucket === "day" ? (
                  <StatCard
                    label={t("dashboard.usageStats.stat.avgCostPerDay")}
                    value={formatCurrency(data.summary.totalCostUsd / Math.max(1, data.series.length))}
                  />
                ) : (
                  <StatCard
                    label={t("dashboard.usageStats.stat.models")}
                    value={formatNumber(data.summary.modelCount)}
                  />
                )}
              </>
            )}
          </div>

          {isEmpty ? (
            <div className="mt-4 flex h-[220px] items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
              {view === "credits" || view === "unitprice"
                ? t("dashboard.usageStats.emptyCredits")
                : t("dashboard.usageStats.empty")}
            </div>
          ) : (
            <>
              <div className="mt-5">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="text-xs font-medium text-muted-foreground">
                    {t(`dashboard.usageStats.chart.title.${view}`)}
                  </div>
                  {view !== "unitprice" ? (
                    <div className="text-xs text-muted-foreground">
                      {t("dashboard.usageStats.chart.total")}:{" "}
                      <span className="font-semibold text-foreground">
                        {view === "cost"
                          ? formatCurrency(data.summary.totalCostUsd)
                          : view === "credits"
                            ? formatCompactNumber(data.summary.totalCredits)
                            : view === "window_credits"
                              ? `${t("dashboard.usage.fiveHourCredits")} ${formatCompactNumber(
                                  windowChartData.primaryTotal,
                                )} · ${t("dashboard.usage.weeklyCredits")} ${formatCompactNumber(
                                  windowChartData.secondaryTotal,
                                )}`
                              : formatCompactNumber(data.summary.totalTokens)}
                      </span>
                    </div>
                  ) : null}
                </div>
                <div className="mt-2">
                  {view === "unitprice" ? (
                    unitPriceRows.length > 0 ? (
                      <Suspense fallback={<div className="h-[260px] rounded-lg bg-muted/30" />}>
                        <UsageStatsUnitPriceChart rows={unitPriceRows} />
                      </Suspense>
                    ) : (
                      <div className="flex h-[220px] items-center justify-center rounded-lg border border-dashed text-sm text-muted-foreground">
                        {t("dashboard.usageStats.emptyCredits")}
                      </div>
                    )
                  ) : view === "window_credits" ? (
                    <Suspense fallback={<div className="h-[260px] rounded-lg bg-muted/30" />}>
                      <UsageStatsWindowChart rows={windowChartData.rows} />
                    </Suspense>
                  ) : (
                    <Suspense fallback={<div className="h-[260px] rounded-lg bg-muted/30" />}>
                      <UsageStatsChart
                        rows={chartData.rows}
                        models={chartData.models}
                        hasOther={chartData.hasOther}
                        names={seriesNames}
                        formatValue={metricValueFormatter}
                      />
                    </Suspense>
                  )}
                </div>
              </div>

              <div
                data-testid="usage-stats-table-scroll"
                className="mt-4 max-h-[17.5rem] overflow-x-auto overflow-y-auto"
              >
                <table className="w-full table-fixed text-sm min-w-[920px]">
                  <colgroup>
                    <col style={{ width: "24%" }} />
                    <col style={{ width: "8%" }} />
                    <col style={{ width: "11%" }} />
                    <col style={{ width: "11%" }} />
                    <col style={{ width: "10%" }} />
                    <col style={{ width: "11%" }} />
                    <col style={{ width: "9%" }} />
                    <col style={{ width: "9%" }} />
                    <col style={{ width: "7%" }} />
                  </colgroup>
                  <thead className="sticky top-0 z-10 bg-card">
                    <tr className="border-b text-left text-muted-foreground">
                      <th className="pb-2 pr-4 font-medium">{t("dashboard.usageStats.table.model")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.requests")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.input")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.output")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.cached")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.total")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.credits")}</th>
                      <th className="pb-2 pr-4 text-right font-medium">{t("dashboard.usageStats.table.cost")}</th>
                      <th className="pb-2 text-right font-medium">{t("dashboard.usageStats.table.share")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {tableRows.map((entry) => (
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
                        <td className="py-2.5 pr-4 text-right text-muted-foreground">
                          <span>{formatCompactNumber(entry.cachedInputTokens)}</span>{" "}
                          <CachedRate rate={cacheHitRate(entry.cachedInputTokens, entry.inputTokens)} />
                        </td>
                        <td className="py-2.5 pr-4 text-right font-medium text-foreground">{formatCompactNumber(entry.totalTokens)}</td>
                        <td className="py-2.5 pr-4 text-right font-medium text-foreground">
                          <span>{entry.credits > 0 ? formatCompactNumber(entry.credits) : "—"}</span>
                          <div className="text-xs text-muted-foreground">
                            {isUsageRatioDisplayable(entry)
                              ? `${usageUnitPriceRatio(entry).toFixed(1)}/1M · n=${entry.attributedRequests}`
                              : "—"}
                          </div>
                        </td>
                        <td className="py-2.5 pr-4 text-right font-medium text-foreground">
                          {entry.costUsd > 0 ? formatCurrency(entry.costUsd) : "—"}
                        </td>
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
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">
                        <span>{formatCompactNumber(data.summary.totalCachedTokens)}</span>{" "}
                        <CachedRate rate={cacheHitRate(data.summary.totalCachedTokens, data.summary.totalInputTokens)} />
                      </td>
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">{formatCompactNumber(data.summary.totalTokens)}</td>
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">
                        {data.summary.totalCredits > 0 ? formatCompactNumber(data.summary.totalCredits) : "—"}
                      </td>
                      <td className="py-2.5 pr-4 text-right font-medium text-foreground">
                        {data.summary.totalCostUsd > 0 ? formatCurrency(data.summary.totalCostUsd) : "—"}
                      </td>
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

function CachedRate({ rate }: { rate: number | null }) {
  if (rate === null) {
    return null;
  }
  return <span className="text-xs text-muted-foreground">({rate}%)</span>;
}
