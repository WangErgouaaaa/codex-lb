// Shared between the panel (main chunk) and the lazily loaded chart modules.
// Keep this file free of recharts imports so it never pulls the chart bundle
// into the main chunk.
export const MAX_VISIBLE_SERIES = 8;
export const OTHER_SERIES_KEY = "__other__";

// A model's credits-per-1M-attributed-tokens ratio is only displayable once
// enough attributed requests have been sampled.
export const MIN_RATIO_SAMPLE_REQUESTS = 20;

export function isUsageRatioDisplayable(entry: {
  attributedRequests: number;
  attributedTokens: number;
}): boolean {
  return entry.attributedRequests >= MIN_RATIO_SAMPLE_REQUESTS && entry.attributedTokens > 0;
}

export function usageUnitPriceRatio(entry: { credits: number; attributedTokens: number }): number {
  return (entry.credits * 1_000_000) / entry.attributedTokens;
}

// Shared per-series palette for the usage charts so both lazy chart modules
// resolve identical colors without cross-importing each other.
export const USAGE_CHART_COLORS: readonly string[] = [
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
];
