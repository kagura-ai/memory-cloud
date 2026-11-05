import { render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SettingsPage from "./page";

const state = vi.hoisted(() => ({ query: "", features: { plan_page: true } as { plan_page: boolean } | null, replace: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: state.replace }), useSearchParams: () => new URLSearchParams(state.query) }));
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemFeatures: () => state.features }));
beforeEach(() => { state.query = ""; state.features = { plan_page: true }; state.replace.mockClear(); });
describe("Settings landing", () => {
  it("opens Plan by default", async () => {
    render(<SettingsPage />);
    await waitFor(() => expect(state.replace).toHaveBeenCalledWith("/workspace/settings/plan"));
  });
  it("uses General when deployment configuration hides Plan", async () => {
    state.features = { plan_page: false }; render(<SettingsPage />);
    await waitFor(() => expect(state.replace).toHaveBeenCalledWith("/workspace/settings/general"));
  });
  it("waits until configuration is available before choosing a default", () => {
    state.features = null; render(<SettingsPage />); expect(state.replace).not.toHaveBeenCalled();
  });
  it("always preserves the workspace-creation entry point and its query", async () => {
    state.features = null; state.query = "create=true"; render(<SettingsPage />);
    await waitFor(() => expect(state.replace).toHaveBeenCalledWith("/workspace/settings/general?create=true"));
  });
});
