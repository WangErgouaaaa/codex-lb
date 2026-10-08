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
let capturedUnitPriceProps: { data?: Array<Record<string, unknown>> } | null = null;
let capturedWindowProps: { data?: Array<Record<string, unknown>> } | null = null;
let capturedBars: Array<{ dataKey?: string; stackId?: string }> = [];
let capturedLines: Array<{ dataKey?: string }> = [];
let capturedLegendFormatter: ((value: unknown) => string) | null = null;

vi.mock("@/components/lazy-recharts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/lazy-recharts")>();

  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    BarChart: (props: {
      children: ReactNode;
      data?: Array<Record<string, unknown>>;
      layout?: string;
    }) => {
      if (props.layout === "vertical") {
        capturedUnitPriceProps = props;
        return <div data-testid="usage-stats-unitprice-chart">{props.children}</div>;
      }
      capturedChartProps = props;
      return <div data-testid="usage-bar-chart">{props.children}</div>;
    },
    Bar: (props: { dataKey?: string; stackId?: string }) => {
      capturedBars.push(props);
      return null;
    },
    LineChart: (props: { children: ReactNode; data?: Array<Record<string, unknown>> }) => {
      capturedWindowProps = props;
      return <div>{props.children}</div>;
    },
    Line: (props: { dataKey?: string }) => {
      capturedLines.push(props);
      return null;
    },
    Cell: () => null,
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
      // totalTokens is input + output; cached tokens are a subset of input.
      totalTokens: 1_200_000,
      totalInputTokens: 1_000_000,
      totalOutputTokens: 200_000,
      totalCachedTokens: 34_567,
      totalRequests: 42,
      totalErrors: 2,
      modelCount: 2,
      avgTokensPerDay: 1_200_000,
      totalCostUsd: 8.05,
      totalCredits: 36.0,
      attributedRequests: 55,
      totalAttributedTokens: 1_150_000,
      totalPrimaryCredits: 0,
      primaryCapacityCredits: 0,
      secondaryCapacityCredits: 0,
      accountCount: 0,
    },
    byModel: [
      {
        model: "gpt-astra",
        requests: 30,
        inputTokens: 900_000,
        outputTokens: 150_000,
        cachedInputTokens: 30_000,
        totalTokens: 1_050_000,
        percentage: 87.5,
        costUsd: 7.75,
        credits: 30.0,
        attributedTokens: 1_000_000,
        attributedRequests: 30,
      },
      {
        model: "gpt-sol",
        requests: 12,
        inputTokens: 100_000,
        outputTokens: 50_000,
        cachedInputTokens: 4_567,
        totalTokens: 150_000,
        percentage: 12.5,
        costUsd: 0.3,
        credits: 6.0,
        attributedTokens: 150_000,
        attributedRequests: 25,
      },
    ],
    series: [
      { bucket: "2026-10-01T09", label: "09:00", values: { "gpt-astra": 450_000, "gpt-sol": 60_000 } },
      { bucket: "2026-10-01T10", label: "10:00", values: { "gpt-astra": 600_000, "gpt-sol": 90_000 } },
    ],
    windowSeries: [],
    accounts: [],
    ...overrides,
  } as UsageStatsResponse;
}

function makeAccountEntry(
  overrides: Partial<UsageStatsResponse["accounts"][number]> = {},
): UsageStatsResponse["accounts"][number] {
  return {
    accountId: "acct-alpha",
    name: "alpha@example.com",
    planType: "pro",
    requests: 30,
    inputTokens: 900_000,
    outputTokens: 150_000,
    cachedInputTokens: 30_000,
    totalTokens: 1_050_000,
    costUsd: 12.5,
    credits: 30.0,
    capacityCredits: 120,
    quotaPercent: 25.0,
    ...overrides,
  };
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
    capturedUnitPriceProps = null;
    capturedWindowProps = null;
    capturedBars = [];
    capturedLines = [];
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
      { label: "09:00", "gpt-astra": 450_000, "gpt-sol": 60_000 },
      { label: "10:00", "gpt-astra": 600_000, "gpt-sol": 90_000 },
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
          attributedTokens: 1_000,
          attributedRequests: 5,
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
          totalAttributedTokens: 0,
          totalPrimaryCredits: 0,
          primaryCapacityCredits: 0,
          secondaryCapacityCredits: 0,
          accountCount: 0,
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

  it("fetches credits and renders the unit price view when the toggle is clicked", async () => {
    mockQueryData(makeResponse({ metric: "credits" }));
    const user = userEvent.setup();

    render(<UsageStatsPanel />);
    expect(await screen.findByTestId("usage-bar-chart")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Unit price (real)" }));

    // The unitprice view is a frontend view served by the credits metric.
    expect(useUsageStatsMock).toHaveBeenLastCalledWith("today", "credits", expect.anything());
    expect(await screen.findByTestId("usage-stats-unitprice-chart")).toBeInTheDocument();
    expect(screen.queryByTestId("usage-bar-chart")).not.toBeInTheDocument();

    expect(screen.getByText("Real unit price by model (credits / 1M tokens)")).toBeInTheDocument();
    // The "total consumption" line is meaningless for a ratio view.
    expect(screen.queryByText(/Total consumption/)).not.toBeInTheDocument();

    // Cards: credits / 1M tokens (36 / 1.15M * 1M = 31.3) and credits per
    // request (36 / 55 = 0.7).
    expect(screen.getByText("Credits / 1M tokens")).toBeInTheDocument();
    expect(screen.getByText("31.3")).toBeInTheDocument();
    expect(screen.getByText("Credits / request")).toBeInTheDocument();
    expect(screen.getByText("0.7")).toBeInTheDocument();
  });

  it("hides ratios below the sample guard and excludes those models from the chart", async () => {
    const base = makeResponse();
    const astra = base.byModel[0];
    const sol = base.byModel[1];
    mockQueryData(
      makeResponse({
        metric: "credits",
        byModel: [
          { ...sol, attributedRequests: 7 }, // below MIN_RATIO_SAMPLE_REQUESTS
          astra,
        ],
      }),
    );
    const user = userEvent.setup();

    render(<UsageStatsPanel />);
    await screen.findByTestId("usage-bar-chart");

    await user.click(screen.getByRole("button", { name: "Unit price (real)" }));
    expect(await screen.findByTestId("usage-stats-unitprice-chart")).toBeInTheDocument();

    // gpt-sol fails the guard: dash subtext instead of a ratio, while
    // gpt-astra still shows its sampled ratio.
    const solRow = screen.getByTestId("usage-stats-row-gpt-sol");
    expect(solRow.textContent).toContain("—");
    expect(solRow.textContent).not.toContain("40.0/1M");
    expect(screen.getByTestId("usage-stats-row-gpt-astra").textContent).toContain("30.0/1M · n=30");

    // Only guarded models are charted.
    expect(capturedUnitPriceProps?.data?.map((row) => String(row.name))).toEqual(["gpt-astra"]);

    // Guarded rows float to the top of the table in the unitprice view even
    // though the server order lists gpt-sol first.
    const rowIds = screen
      .getAllByTestId(/^usage-stats-row-/)
      .map((row) => row.getAttribute("data-testid"));
    expect(rowIds).toEqual(["usage-stats-row-gpt-astra", "usage-stats-row-gpt-sol"]);
  });

  it("orders the unit price chart with the cheapest model on top", async () => {
    mockQueryData(makeResponse({ metric: "credits" })); // gpt-astra 30.0 < gpt-sol 40.0
    const user = userEvent.setup();

    render(<UsageStatsPanel />);
    await screen.findByTestId("usage-bar-chart");

    await user.click(screen.getByRole("button", { name: "Unit price (real)" }));
    expect(await screen.findByTestId("usage-stats-unitprice-chart")).toBeInTheDocument();

    // A recharts vertical category axis lists the first data entry at the
    // bottom, so the cheapest model must be the last row to sit on top.
    expect(capturedUnitPriceProps?.data?.map((row) => String(row.name))).toEqual([
      "gpt-sol",
      "gpt-astra",
    ]);
  });
  it("renders the window share view with both quota windows as percent lines", async () => {
    mockQueryData(
      makeResponse({
        metric: "window_credits",
        series: [
          { bucket: "2026-10-01T09", label: "09:00", values: {} },
          { bucket: "2026-10-01T10", label: "10:00", values: {} },
        ],
        windowSeries: [
          { bucket: "2026-10-01T09", label: "09:00", primaryCredits: 30, secondaryCredits: 60 },
          { bucket: "2026-10-01T10", label: "10:00", primaryCredits: 10, secondaryCredits: 20 },
        ],
        summary: {
          ...makeResponse().summary,
          totalPrimaryCredits: 40,
          totalCredits: 80,
          primaryCapacityCredits: 40,
          secondaryCapacityCredits: 80,
        },
      }),
    );
    const user = userEvent.setup();

    render(<UsageStatsPanel />);
    // The mocked response belongs to the window_credits metric, so the model
    // series is empty and the initial tokens view shows its empty state.
    expect(await screen.findByText("No usage data in the selected range")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Window share" }));
    expect(useUsageStatsMock).toHaveBeenLastCalledWith("today", "window_credits", expect.anything());
    expect(await screen.findByTestId("usage-stats-window-chart")).toBeInTheDocument();
    expect(screen.queryByTestId("usage-bar-chart")).not.toBeInTheDocument();

    // Each line is a share of the pool's window capacity: 30/40 = 75%,
    // 60/80 = 75% in the first bucket and 25% in the second.
    expect(capturedWindowProps?.data).toEqual([
      { label: "09:00", primaryPercent: 75, secondaryPercent: 75, primaryCredits: 30, secondaryCredits: 60 },
      { label: "10:00", primaryPercent: 25, secondaryPercent: 25, primaryCredits: 10, secondaryCredits: 20 },
    ]);
    expect(capturedLines.map((line) => line.dataKey)).toEqual(["primaryPercent", "secondaryPercent"]);

    // Cards expose both window totals with their pool capacity denominators.
    expect(screen.getByText("5-Hour Credits")).toBeInTheDocument();
    expect(screen.getByText("Weekly Credits")).toBeInTheDocument();
    expect(screen.getByText("Pool capacity: 40")).toBeInTheDocument();
    expect(screen.getByText("Pool capacity: 80")).toBeInTheDocument();
    expect(screen.getByTestId("usage-stats-row-gpt-astra")).toBeInTheDocument();
    expect(
      screen.getByText("Quota window consumption (5-Hour vs Weekly, % of pool capacity)"),
    ).toBeInTheDocument();
  });

  it("fetches the accounts metric and renders per-account cards, chart, and table", async () => {
    mockQueryData(
      makeResponse({
        metric: "accounts",
        series: [],
        accounts: [
          makeAccountEntry(),
          makeAccountEntry({
            accountId: "acct-beta",
            name: "beta@example.com",
            planType: "max",
            requests: 12,
            inputTokens: 100_000,
            outputTokens: 50_000,
            cachedInputTokens: 4_567,
            totalTokens: 150_000,
            credits: 6.0,
            capacityCredits: 0,
            quotaPercent: 0,
          }),
        ],
        summary: {
          ...makeResponse().summary,
          accountCount: 2,
          secondaryCapacityCredits: 120,
        },
      }),
    );
    const user = userEvent.setup();

    render(<UsageStatsPanel />);
    // The mocked response belongs to the accounts metric, so the model series
    // is empty and the initial tokens view shows its empty state.
    expect(await screen.findByText("No usage data in the selected range")).toBeInTheDocument();

    // "accounts" is a real API metric, not a frontend view like unitprice.
    await user.click(screen.getByRole("button", { name: "Accounts" }));
    expect(useUsageStatsMock).toHaveBeenLastCalledWith("today", "accounts", expect.anything());

    expect(await screen.findByTestId("usage-stats-accounts-chart")).toBeInTheDocument();
    expect(screen.getByText("Token usage by account")).toBeInTheDocument();

    // Cards: active account count plus the shared token/credit totals and the
    // summed weekly capacity.
    const cards = screen.getAllByTestId("usage-stats-stat-card");
    expect(cards).toHaveLength(4);
    expect(cards[0].textContent).toContain("Active accounts");
    expect(cards[0].textContent).toContain("2");
    expect(cards[3].textContent).toContain("Weekly capacity (credits)");
    expect(cards[3].textContent).toContain("120");

    // One horizontal bar per account sized by total tokens, server order kept.
    expect(capturedUnitPriceProps?.data?.map((row) => String(row.name))).toEqual([
      "alpha@example.com",
      "beta@example.com",
    ]);
    expect(capturedBars.map((bar) => bar.dataKey)).toEqual(["tokens"]);

    // Table rows keyed by account: name, plan sub-line, tokens, credits.
    const alphaRow = screen.getByTestId("usage-stats-row-acct-alpha");
    expect(alphaRow.textContent).toContain("alpha@example.com");
    expect(alphaRow.textContent).toContain("pro");
    expect(alphaRow.textContent).toContain("1.05M");
    expect(alphaRow.textContent).toContain("87.5%"); // 1.05M / 1.2M
    expect(screen.getByTestId("usage-stats-row-acct-beta").textContent).toContain("beta@example.com");
  });

  it("shows the empty state for the accounts view when no accounts matched", async () => {
    mockQueryData(makeResponse({ metric: "accounts", series: [], accounts: [] }));
    const user = userEvent.setup();

    render(<UsageStatsPanel />);
    expect(await screen.findByText("No usage data in the selected range")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Accounts" }));
    expect(useUsageStatsMock).toHaveBeenLastCalledWith("today", "accounts", expect.anything());

    expect(screen.getByText("No usage data in the selected range")).toBeInTheDocument();
    expect(screen.queryByTestId("usage-stats-accounts-chart")).not.toBeInTheDocument();
    expect(screen.queryByTestId("usage-stats-table-scroll")).not.toBeInTheDocument();
  });

  it("renders the weekly quota share only for accounts with capacity", async () => {
    mockQueryData(
      makeResponse({
        metric: "accounts",
        series: [],
        accounts: [
          makeAccountEntry(), // quotaPercent = 30 / 120 * 100 = 25.0
          makeAccountEntry({
            accountId: "acct-beta",
            name: "beta@example.com",
            planType: "max",
            requests: 12,
            inputTokens: 100_000,
            outputTokens: 50_000,
            cachedInputTokens: 4_567,
            totalTokens: 150_000,
            credits: 6.0,
            capacityCredits: 0,
            quotaPercent: 0,
          }),
        ],
      }),
    );
    const user = userEvent.setup();

    render(<UsageStatsPanel />);
    expect(await screen.findByText("No usage data in the selected range")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Accounts" }));
    expect(await screen.findByTestId("usage-stats-accounts-chart")).toBeInTheDocument();

    const alphaRow = screen.getByTestId("usage-stats-row-acct-alpha");
    expect(alphaRow.textContent).toContain("30"); // credits
    expect(alphaRow).toHaveTextContent("Weekly quota 25.0%");

    // Without capacity there is no quota share to derive: the credits cell
    // keeps a dash sub-line while the credits total still renders.
    const betaRow = screen.getByTestId("usage-stats-row-acct-beta");
    expect(betaRow.textContent).toContain("6");
    expect(betaRow.textContent).not.toContain("Weekly quota");
  });
});
