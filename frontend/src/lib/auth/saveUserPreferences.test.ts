import { describe, expect, it, vi, beforeEach } from "vitest";
import { saveUserPreferences } from "./saveUserPreferences";
import { updateUserProfile } from "@/lib/api/base";
import type { User } from "@/lib/auth/auth";

vi.mock("@/lib/api/base", () => ({ updateUserProfile: vi.fn() }));
beforeEach(() => vi.mocked(updateUserProfile).mockReset());

describe("saving UI preferences", () => {
  it.each([undefined, null])("does not apply a locale over a stale user after refetch returns %s", async (fresh) => {
    const apply = vi.fn();
    await expect(saveUserPreferences({ locale: "ja" }, vi.fn().mockResolvedValue(fresh), apply)).rejects.toThrow();
    expect(updateUserProfile).toHaveBeenCalledWith({ locale: "ja" });
    expect(apply).not.toHaveBeenCalled();
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
