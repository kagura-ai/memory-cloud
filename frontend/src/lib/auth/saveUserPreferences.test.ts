import { describe, expect, it, vi, beforeEach } from "vitest";
import { PreferencesRefreshError, saveUserPreferences } from "./saveUserPreferences";
import { updateUserProfile } from "@/lib/api/base";
import type { User } from "@/lib/auth/auth";

vi.mock("@/lib/api/base", () => ({ updateUserProfile: vi.fn() }));
beforeEach(() => vi.mocked(updateUserProfile).mockReset());

describe("saving UI preferences", () => {
  it.each([undefined, null])("does not apply a locale over a stale user after refetch returns %s", async (fresh) => {
    const apply = vi.fn();
    await expect(saveUserPreferences({ locale: "ja" }, vi.fn().mockResolvedValue(fresh), apply)).rejects.toBeInstanceOf(PreferencesRefreshError);
    expect(updateUserProfile).toHaveBeenCalledWith({ locale: "ja" });
    expect(apply).not.toHaveBeenCalled();
  });

  it("lets a failed save surface as itself, not as a refresh failure", async () => {
    vi.mocked(updateUserProfile).mockRejectedValueOnce(new Error("Invalid timezone"));
    const refetch = vi.fn();
    const failure = saveUserPreferences({ locale: "ja" }, refetch, vi.fn());
    await expect(failure).rejects.not.toBeInstanceOf(PreferencesRefreshError);
    expect(refetch).not.toHaveBeenCalled();
  });

  it("applies the chosen language only after refreshing the saved user", async () => {
    const order: string[] = [];
    vi.mocked(updateUserProfile).mockImplementation(async () => {
      order.push("put"); return {} as Awaited<ReturnType<typeof updateUserProfile>>;
    });
    await saveUserPreferences({ locale: "ja", timezone: "Asia/Tokyo" }, async () => {
      order.push("refetch"); return { locale: "ja" } as User;
    }, () => { order.push("apply"); });
    expect(order).toEqual(["put", "refetch", "apply"]);
  });
});
