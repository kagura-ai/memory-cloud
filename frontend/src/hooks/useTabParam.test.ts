import { renderHook, act } from "@testing-library/react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { useTabParam, resolveTabValue } from "./useTabParam";

const { mockGet, mockReplace, mockToString } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockReplace: vi.fn(),
  mockToString: vi.fn(() => ""),
}));

vi.mock("next/navigation", () => ({
  useSearchParams: () => ({ get: mockGet, toString: mockToString }),
  usePathname: () => "/test",
  useRouter: () => ({ replace: mockReplace }),
}));

// Force NODE_ENV=development so dev-only console.warn branches fire under
// vitest (which defaults to NODE_ENV='test'). Matches the existing convention
// in components like ResourceTokensTabPanel that gate dev logs on
// `process.env.NODE_ENV === 'development'`.
beforeEach(() => {
  vi.stubEnv("NODE_ENV", "development");
  mockGet.mockReset();
  mockReplace.mockReset();
  mockToString.mockReset().mockReturnValue("");
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("useTabParam", () => {
  it("returns defaultValue when the search param is absent", () => {
    mockGet.mockReturnValue(null);
    const { result } = renderHook(() => useTabParam("overview"));
    expect(result.current[0]).toBe("overview");
  });

  it("returns the URL value when present and no allowedValues", () => {
    mockGet.mockReturnValue("settings");
    const { result } = renderHook(() => useTabParam("overview"));
    expect(result.current[0]).toBe("settings");
  });

  it("setValue removes the param from the URL when called with defaultValue", () => {
    mockGet.mockReturnValue("settings");
    mockToString.mockReturnValue("tab=settings");
    const { result } = renderHook(() => useTabParam("overview"));

    act(() => {
      result.current[1]("overview");
    });

    expect(mockReplace).toHaveBeenCalledWith("/test");
  });

  it("setValue preserves unrelated existing params", () => {
    mockGet.mockReturnValue(null);
    mockToString.mockReturnValue("foo=bar");
    const { result } = renderHook(() => useTabParam("overview"));

    act(() => {
      result.current[1]("settings");
    });

    expect(mockReplace).toHaveBeenCalledWith("/test?foo=bar&tab=settings");
  });

  it("falls back to defaultValue and warns when URL value is not in allowedValues", () => {
    mockGet.mockReturnValue("hacked");
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    const { result } = renderHook(() =>
      useTabParam("overview", "tab", ["overview", "settings"]),
    );

    expect(result.current[0]).toBe("overview");
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0][0]).toContain('"hacked"');
    warnSpy.mockRestore();
  });

  it("returns URL value when it is in allowedValues", () => {
    mockGet.mockReturnValue("settings");
    const { result } = renderHook(() =>
      useTabParam("overview", "tab", ["overview", "settings"]),
    );
    expect(result.current[0]).toBe("settings");
  });

  it("treats an empty allowedValues array as no validation (backward compat)", () => {
    mockGet.mockReturnValue("anything");
    const { result } = renderHook(() => useTabParam("overview", "tab", []));
    expect(result.current[0]).toBe("anything");
  });

  it("warns in dev when defaultValue is not in allowedValues", () => {
    mockGet.mockReturnValue(null);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    renderHook(() => useTabParam("not-listed", "tab", ["a", "b"]));

    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0][0]).toContain("not-listed");
    warnSpy.mockRestore();
  });
});

describe("resolveTabValue", () => {
  it("returns defaultValue when raw is null", () => {
    expect(resolveTabValue(null, "default", undefined, "tab")).toBe("default");
    expect(resolveTabValue(null, "default", ["a", "b"], "tab")).toBe("default");
  });

  it("returns raw when allowedValues is undefined", () => {
    expect(resolveTabValue("anything", "default", undefined, "tab")).toBe(
      "anything",
    );
  });

  it("returns raw when allowedValues is empty", () => {
    expect(resolveTabValue("anything", "default", [], "tab")).toBe("anything");
  });

  it("returns raw when it is in allowedValues", () => {
    expect(resolveTabValue("a", "default", ["a", "b"], "tab")).toBe("a");
  });

  it("returns defaultValue and warns when raw is not in allowedValues", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(resolveTabValue("c", "default", ["a", "b"], "tab")).toBe("default");
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(warnSpy.mock.calls[0][0]).toContain('"c"');
    expect(warnSpy.mock.calls[0][0]).toContain("tab");
    warnSpy.mockRestore();
  });
});
