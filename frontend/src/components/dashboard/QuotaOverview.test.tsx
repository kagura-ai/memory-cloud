import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ja from "@/messages/ja.json";
import { QuotaNotice, QuotaOverview } from "./QuotaOverview";
import type { QuotaUsage } from "@/lib/api/usage";

let canUpgrade: boolean | null = true;
let planPage = true;
const push = vi.fn();
vi.mock("@/hooks/useCanUpgrade", () => ({ useCanUpgrade: () => canUpgrade }));
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemFeatures: () => ({plan_page: planPage}) }));
vi.mock("next/navigation", () => ({ useRouter: () => ({push}) }));
const meter = (override: Partial<QuotaUsage> = {}): QuotaUsage => ({
  id: "memories", used: 80, limit: 100, bonus: 0, unit: "count", scope: "workspace", period: "current", ...override
});
function show(component: React.ReactNode) {
  return render(<NextIntlClientProvider locale="ja" messages={ja}>{component}</NextIntlClientProvider>);
}
beforeEach(() => { canUpgrade = true; planPage = true; push.mockClear(); });

describe("quota dashboard", () => {
  it.each([79, 80, 100])("shows an owner CTA from 80 percent (%s)", used => {
    show(<QuotaNotice meters={[meter({used})]} />);
    const button = screen.queryByRole("button", {name: ja.usageStats.reviewPlan});
    expect(Boolean(button)).toBe(used >= 80);
    if (button) {
      fireEvent.click(button);
      expect(push).toHaveBeenCalledWith("/workspace/settings/plan");
    }
  });
  it("shows owner guidance to shared members", () => {
    canUpgrade = false;
    show(<QuotaNotice meters={[meter()]} />);
    expect(screen.getByText(ja.usageStats.askOwner)).toBeInTheDocument();
    expect(screen.queryByRole("button", {name: ja.usageStats.reviewPlan})).toBeNull();
  });
  it("respects the deployment plan gate", () => {
    canUpgrade = false;
    planPage = false;
    show(<QuotaNotice meters={[meter()]} />);
    expect(screen.queryByText(ja.usageStats.askOwner)).toBeNull();
    expect(screen.queryByRole("button", {name: ja.usageStats.reviewPlan})).toBeNull();
  });
  it("reports usage against a zero allowance as reached, without a percentage", () => {
    show(<QuotaNotice meters={[meter({id: "connectors", used: 3, limit: 0}), meter({id: "analysis", used: 0, limit: 0})]} />);
    expect(screen.getByText(ja.usageStats.quotaReached)).toBeInTheDocument();
    const summary = screen.getByText(new RegExp(ja.usageStats.quotaLabels.connectors));
    expect(summary.textContent).toContain(ja.usageStats.quotaNotIncluded);
    expect(summary.textContent).not.toContain(ja.usageStats.quotaLabels.analysis);
    expect(document.body.textContent).not.toMatch(/NaN|Infinity/);
  });
  it("does not apply workspace upgrade guidance to account or spending limits", () => {
    show(<QuotaNotice meters={[meter({scope:"account"}), meter({unit:"usd"})]} />);
    expect(screen.queryByRole("alert")).toBeNull();
  });
  it("separates account and key limits and renders zero and uncapped meters", () => {
    show(<QuotaOverview meters={[
      meter({limit:0,used:0}), meter({id:"content_chunks",limit:null}),
      meter({id:"workspaces",scope:"account"}), meter({id:"bound_key_rate",scope:"api_key",used:null,period:"minute"}),
    ]} />);
    expect(screen.getByRole("heading", {name:ja.usageStats.quotaGroups.account})).toBeInTheDocument();
    expect(screen.getByRole("heading", {name:ja.usageStats.quotaGroups.apiKey})).toBeInTheDocument();
    expect(screen.getByText(ja.usageStats.quotaNotIncluded)).toBeInTheDocument();
    expect(screen.getByText(/上限なし/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/NaN|Infinity/);
  });
});
