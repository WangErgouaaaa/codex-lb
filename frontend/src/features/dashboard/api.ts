import { get } from "@/lib/api-client";

import {
  DEFAULT_OVERVIEW_TIMEFRAME,
  DashboardOverviewSchema,
  DashboardProjectionsSchema,
  RequestLogFilterOptionsSchema,
  RequestLogsResponseSchema,
  UsageStatsResponseSchema,
  type OverviewTimeframe,
  type UsageStatsMetric,
  type UsageStatsRange,
} from "@/features/dashboard/schemas";

const DASHBOARD_PATH = "/api/dashboard";
const REQUEST_LOGS_PATH = "/api/request-logs";
const REPORTS_PATH = "/api/reports";

export type RequestLogsListFilters = {
  limit?: number;
  offset?: number;
  search?: string;
  accountIds?: string[];
  apiKeyIds?: string[];
  statuses?: string[];
  modelOptions?: string[];
  since?: string;
  until?: string;
  conversationId?: string;
};

export type RequestLogFacetFilters = {
  since?: string;
  until?: string;
  accountIds?: string[];
  apiKeyIds?: string[];
  modelOptions?: string[];
};

export type DashboardOverviewParams = {
  timeframe?: OverviewTimeframe;
};

function appendMany(params: URLSearchParams, key: string, values?: string[]): void {
  if (!values || values.length === 0) {
    return;
  }
  for (const value of values) {
    if (value) {
      params.append(key, value);
    }
  }
}

export function getDashboardOverview(params: DashboardOverviewParams = {}) {
  const query = new URLSearchParams();
  query.set("timeframe", params.timeframe ?? DEFAULT_OVERVIEW_TIMEFRAME);
  return get(`${DASHBOARD_PATH}/overview?${query.toString()}`, DashboardOverviewSchema);
}

export function getDashboardProjections() {
  return get(`${DASHBOARD_PATH}/projections`, DashboardProjectionsSchema);
}

export function getRequestLogs(params: RequestLogsListFilters = {}) {
  const query = new URLSearchParams();
  if (typeof params.limit === "number") {
    query.set("limit", String(params.limit));
  }
  if (typeof params.offset === "number") {
    query.set("offset", String(params.offset));
  }
  if (params.search) {
    query.set("search", params.search);
  }
  appendMany(query, "accountId", params.accountIds);
  appendMany(query, "apiKeyId", params.apiKeyIds);
  appendMany(query, "status", params.statuses);
  appendMany(query, "modelOption", params.modelOptions);
  if (params.conversationId) {
    query.set("conversation_id", params.conversationId);
  }
  if (params.since) {
    query.set("since", params.since);
  }
  if (params.until) {
    query.set("until", params.until);
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return get(`${REQUEST_LOGS_PATH}${suffix}`, RequestLogsResponseSchema);
}

export type UsageStatsParams = {
  range?: UsageStatsRange;
  metric?: UsageStatsMetric;
  timezone?: string;
};

export function getUsageStats(params: UsageStatsParams = {}) {
  const query = new URLSearchParams();
  query.set("range", params.range ?? "7d");
  query.set("metric", params.metric ?? "tokens");
  if (params.timezone) {
    query.set("timezone", params.timezone);
  }
  return get(`${REPORTS_PATH}/usage-stats?${query.toString()}`, UsageStatsResponseSchema);
}

export function getRequestLogOptions(params: RequestLogFacetFilters = {}) {
  const query = new URLSearchParams();
  if (params.since) {
    query.set("since", params.since);
  }
  if (params.until) {
    query.set("until", params.until);
  }
  appendMany(query, "accountId", params.accountIds);
  appendMany(query, "apiKeyId", params.apiKeyIds);
  appendMany(query, "modelOption", params.modelOptions);
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return get(`${REQUEST_LOGS_PATH}/options${suffix}`, RequestLogFilterOptionsSchema);
}
