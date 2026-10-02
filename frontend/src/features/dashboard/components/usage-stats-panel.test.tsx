import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useUsageStats } from "@/features/dashboard/hooks/use-usage-stats";
import { UsageStatsPanel } from "@/features/dashboard/components/usage-stats-panel";
import type { UsageStatsResponse } from "@/features/dashboard/schemas";

vi.mock("@/features/dashboard/hooks/use-usage-stats", () => ({
  useUsageStats: vi.fn(),
}));

let capturedChartProps: { data?: Array<Record<string, unknown>> } | null = null;
let capturedBars: Array<{ dataKey?: string; stackId?: string }> = [];
let capturedLegendFormatter: ((value: unknown) => string) | null = null;

vi.mock("@/components/lazy-recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/lazy-recharts")>();

  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    BarChart: (props: { children: ReactNode; data?: Array<Record<string, unknown>> }) => {
      capturedChartProps = props;
      return <div data-testid="usage-bar-chart">{props.children}</div>;
    },
    Bar: (props: { dataKey?: string; stackId?: string }) => {
      capturedBars.push(props);
      return null;
    },
    XAxis: () => null,
    YAxis: () => null,
    CartesianGrid: () => null,
    Tooltip: () => null,
    Legend: (props: { formatter?: (value: unknown) => string }) => {
      capturedLegendFormatter = props.formatter ?? null;
      return null;
    },
  };
});

const useUsageStatsMock = vi.mocked(useUsageStats);

function makeResponse(overrides: Partial<UsageStatsResponse> = {}): UsageStatsResponse {
  return {
    range: "today",
    bucket: "hour",
    timezone: "UTC",
    startDate: "2026-10-01",
    endDate: "2026-10-01",
    metric: "tokens",
    summary: {
      totalTokens: 1_234_567,
      totalInputTokens: 1_000_000,
      totalOutputTokens: 200_000,
      totalCachedTokens: 34_567,
      totalRequests: 42,
      totalErrors: 2,
      modelCount: 2,
      avgTokensPerDay: 1_234_567,
      totalCostUsd: 8.05,
      totalCredits: 36.0,
      attributedRequests: 10,
    },
    byModel: [
      {
        model: "gpt-astra",
        requests: 30,
        inputTokens: 900_000,
        outputTokens: 150_000,
        cachedInputTokens: 30_000,
        totalTokens: 1_080_000,
        percentage: 87.5,
        costUsd: 7.75,
        credits: 30.0,
      },
      {
        model: "gpt-sol",
        requests: 12,
        inputTokens: 100_000,
        outputTokens: 50_000,
        cachedInputTokens: 4_567,
        totalTokens: 154_567,
        percentage: 12.5,
        costUsd: 0.3,
        credits: 6.0,
      },
    ],
    series: [
      { bucket: "2026-10-01T09", label: "09:00", values: { "gpt-astra": 500_000, "gpt-sol": 100_000 } },
      { bucket: "2026-10-01T10", label: "10:00", values: { "gpt-astra": 580_000, "gpt-sol": 54_567 } },
    ],
    ...overrides,
  } as UsageStatsResponse;
}

function mockQueryData(data: UsageStatsResponse | undefined) {
  useUsageStatsMock.mockReturnValue({
    data,
    error: null,
    isPending: data === undefined,
    isFetching: false,
    refetch: vi.fn(),
  } as unknown as ReturnType<typeof useUsageStats>);
}

describe("UsageStatsPanel", () => {
  beforeEach(() => {
    capturedChartProps = null;
    capturedBars = [];
    capturedLegendFormatter = null;
    useUsageStatsMock.mockReset();
  });

  it("renders summary cards, stacked chart series, and per-model table", async () => {
    mockQueryData(makeResponse());

    render(<UsageStatsPanel />);

    expect(await screen.findByTestId("usage-bar-chart")).toBeInTheDocument();
    expect(screen.getByText("Usage Stats")).toBeInTheDocument();
    expect(screen.getByText("Total tokens")).toBeInTheDocument();
    // "Requests" appears both as a summary card and as a table column.
    expect(screen.getAllByText("Requests").length).toBeGreaterThanOrEqual(2);

    // Table lists every model plus the totals footer.
    expect(screen.getByTestId("usage-stats-row-gpt-astra")).toBeInTheDocument();
    expect(screen.getByTestId("usage-stats-row-gpt-sol")).toBeInTheDocument();
    expect(screen.getByText("87.5%")).toBeInTheDocument();

    // Cached column carries the per-model cache hit rate (cached / input).
    expect(screen.getByTestId("usage-stats-row-gpt-astra").textContent).toContain("(3%)");
    expect(screen.getByTestId("usage-stats-row-gpt-sol").textContent).toContain("(5%)");

    // Chart rows keep bucket labels and per-model values for stacking.
    expect(capturedChartProps?.data).toEqual([
      { label: "09:00", "gpt-astra": 500_000, "gpt-sol": 100_000 },
      { label: "10:00", "gpt-astra": 580_000, "gpt-sol": 54_567 },
    ]);
    expect(capturedBars.map((bar) => bar.dataKey)).toEqual(["gpt-astra", "gpt-sol"]);
    expect(capturedBars.every((bar) => bar.stackId === "usage")).toBe(true);

    // Legend formatter localizes the synthetic "other" bucket.
    expect(capturedLegendFormatter?.("__other__")).toBe("Other");
    expect(capturedLegendFormatter?.("gpt-astra")).toBe("gpt-astra");
  });

  it("merges models beyond the visible limit into the other bucket", async () => {
    const models = Array.from({ length: 10 }, (_, index) => `model-${index + 1}`);
    const values = Object.fromEntries(models.map((model) => [model, 1_000]));
    mockQueryData(
      makeResponse({
        bucket: "day",
        byModel: models.map((model) => ({
          model,
          requests: 5,
          inputTokens: 800,
          outputTokens: 150,
          cachedInputTokens: 50,
          totalTokens: 1_000,
          percentage: 10,
          costUsd: 0.1,
          credits: 1.5,
        })),
        series: [{ bucket: "2026-10-01", label: "10-01", values }],
      }),
    );

    render(<UsageStatsPanel />);

    expect(await screen.findByTestId("usage-bar-chart")).toBeInTheDocument();
    expect(capturedBars.map((bar) => bar.dataKey)).toEqual([
      "model-1",
      "model-2",
      "model-3",
      "model-4",
      "model-5",
      "model-6",
      "model-7",
      "model-8",
      "__other__",
    ]);
    const chartRow = capturedChartProps?.data?.[0] as Record<string, unknown>;
    expect(chartRow["__other__"]).toBe(2_000); // model-9 + model-10
  });

  it("switches the requested range when tabs are clicked", async () => {
    mockQueryData(makeResponse());
    const user = userEvent.setup();

    render(<UsageStatsPanel />);

    expect(useUsageStatsMock).toHaveBeenNthCalledWith(1, "today", "tokens", expect.anything());

    await user.click(screen.getByRole("button", { name: "Last 7 days" }));
    expect(useUsageStatsMock).toHaveBeenLastCalledWith("7d", "tokens", expect.anything());

    await user.click(screen.getByRole("button", { name: "Last 30 days" }));
    expect(useUsageStatsMock).toHaveBeenLastCalledWith("30d", "tokens", expect.anything());
  });

  it("switches the metric and renders credits and cost columns", async () => {
    mockQueryData(makeResponse());
    const user = userEvent.setup();

    render(<UsageStatsPanel />);

    // Token metric is the default; table carries the credits and cost columns.
    expect(await screen.findByText("Token usage by model (stacked)")).toBeInTheDocument();
    expect(screen.getAllByText("Credits").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Cost")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Credits (real)" }));
    expect(useUsageStatsMock).toHaveBeenLastCalledWith("today", "credits", expect.anything());

    await user.click(screen.getByRole("button", { name: "Cost (API-equiv.)" }));
    expect(useUsageStatsMock).toHaveBeenLastCalledWith("today", "cost", expect.anything());
  });

  it("shows the empty state when the range has no usage", async () => {
    mockQueryData(
      makeResponse({
        summary: {
          totalTokens: 0,
          totalInputTokens: 0,
          totalOutputTokens: 0,
          totalCachedTokens: 0,
          totalRequests: 0,
          totalErrors: 0,
          modelCount: 0,
          avgTokensPerDay: 0,
          totalCostUsd: 0,
          totalCredits: 0,
          attributedRequests: 0,
        },
        byModel: [],
        series: [{ bucket: "2026-10-01T00", label: "00:00", values: {} }],
      }),
    );

    render(<UsageStatsPanel />);

    expect(await screen.findByText("No usage data in the selected range")).toBeInTheDocument();
    expect(screen.queryByTestId("usage-bar-chart")).not.toBeInTheDocument();
    expect(screen.queryByTestId("usage-stats-table-scroll")).not.toBeInTheDocument();
  });
});
