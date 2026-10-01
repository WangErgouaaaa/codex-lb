// Shared between the panel (main chunk) and the lazily loaded chart module.
// Keep this file free of recharts imports so it never pulls the chart bundle
// into the main chunk.
export const MAX_VISIBLE_SERIES = 8;
export const OTHER_SERIES_KEY = "__other__";
