/**
 * Stable test IDs for the workspace dashboard. Shared between the components
 * and the Playwright specs so renames are caught at compile time, and so E2E
 * locators stay locale-independent (testid only, never visible text — #688).
 */
export const DASHBOARD_TEST_IDS = {
  /** The KPI card grid: rendered only once the stats have loaded (#1824). */
  kpiCards: "kpi-cards",
} as const;
