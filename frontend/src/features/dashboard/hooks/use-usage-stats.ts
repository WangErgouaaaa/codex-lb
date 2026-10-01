import { useQuery } from "@tanstack/react-query";

import { getUsageStats } from "@/features/dashboard/api";
import type { UsageStatsMetric, UsageStatsRange } from "@/features/dashboard/schemas";

export function useUsageStats(
  range: UsageStatsRange,
  metric: UsageStatsMetric,
  timeZone: string | undefined,
) {
  return useQuery({
    queryKey: ["usage-stats", range, metric, timeZone],
    queryFn: () => getUsageStats({ range, metric, timezone: timeZone }),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });
}
