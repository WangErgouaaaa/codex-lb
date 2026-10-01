import { useQuery } from "@tanstack/react-query";

import { getUsageStats } from "@/features/dashboard/api";
import type { UsageStatsRange } from "@/features/dashboard/schemas";

export function useUsageStats(range: UsageStatsRange, timeZone: string | undefined) {
  return useQuery({
    queryKey: ["usage-stats", range, timeZone],
    queryFn: () => getUsageStats({ range, timezone: timeZone }),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });
}
