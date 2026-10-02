import { describe, expect, it } from "vitest";
import { contextOwnerKind, contextOwnerLabel } from "./contextOwner";

const labels = { you: "You", unnamed: "Another member" };

describe("contextOwnerKind", () => {
  it("is mine when the viewer created it", () => {
    expect(contextOwnerKind("u1", "u1")).toBe("mine");
  });
  it("is shared when someone else did", () => {
    expect(contextOwnerKind("u2", "u1")).toBe("shared");
  });
  it("is mine when an account linked to the viewer created the context (#1784)", () => {
    expect(contextOwnerKind("u2", "u1", ["u2", "u3"])).toBe("mine");
  });
  it("is shared when the creator is not among the linked accounts", () => {
    expect(contextOwnerKind("u4", "u1", ["u2", "u3"])).toBe("shared");
    expect(contextOwnerKind("u2", "u1", [])).toBe("shared");
    expect(contextOwnerKind("u2", "u1", null)).toBe("shared");
  });
  it("stays unknown without a viewer even when linked ids are given", () => {
    expect(contextOwnerKind("u2", undefined, ["u2"])).toBe("unknown");
    expect(contextOwnerKind(null, "u1", ["u2"])).toBe("unknown");
  });
  it("is unknown without a creator", () => {
    expect(contextOwnerKind(null, "u1")).toBe("unknown");
    expect(contextOwnerKind(undefined, "u1")).toBe("unknown");
  });
  it("is unknown while the viewer is not loaded, even for a creator that would match", () => {
    expect(contextOwnerKind("u1", undefined)).toBe("unknown");
    expect(contextOwnerKind("u1", null)).toBe("unknown");
  });
});

describe("contextOwnerLabel", () => {
  it("says You for mine regardless of the stored name", () => {
    expect(contextOwnerLabel("mine", "Me Myself", labels)).toBe("You");
  });
  it("names a shared creator, or stands in when they have no display name", () => {
    expect(contextOwnerLabel("shared", "Bob", labels)).toBe("Bob");
    expect(contextOwnerLabel("shared", null, labels)).toBe("Another member");
    expect(contextOwnerLabel("shared", "", labels)).toBe("Another member");
  });
  it("shows a dash when nothing is known", () => {
    expect(contextOwnerLabel("unknown", "Bob", labels)).toBe("—");
  });
});
