import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";

import { MemoryContextProvider, useMemoryContext } from "./MemoryContextContext";

const CTX = "11111111-1111-4111-8111-111111111111";

let mockQuery = "";
vi.mock("next/navigation", () => ({
  useSearchParams: () => new URLSearchParams(mockQuery),
}));

const mockGetContext = vi.fn();
vi.mock("@/lib/api/contexts", () => ({
  getContext: (...args: unknown[]) => mockGetContext(...args),
}));

function Probe() {
  const { contextId, isLoading } = useMemoryContext();
  return <div data-testid="probe">{isLoading ? "loading" : String(contextId)}</div>;
}

function renderProvider() {
  return render(
    <MemoryContextProvider>
      <Probe />
    </MemoryContextProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockQuery = "";
  mockGetContext.mockResolvedValue({ id: CTX, name: "Alpha" });
});

afterEach(() => {
  cleanup();
});

describe("MemoryContextProvider context parameter", () => {
  it("reads the context parameter when it is a UUID", async () => {
    mockQuery = `context=${CTX}`;
    const { getByTestId } = renderProvider();

    await waitFor(() => expect(getByTestId("probe").textContent).toBe(CTX));
    expect(mockGetContext).toHaveBeenCalledWith(CTX);
  });

  it("reads the context parameter only when it is a UUID", async () => {
    mockQuery = "context=../admin";
    const { getByTestId } = renderProvider();

    await waitFor(() => expect(getByTestId("probe").textContent).toBe("null"));
    expect(mockGetContext).not.toHaveBeenCalled();
  });

  it("treats a missing context parameter as absent", async () => {
    const { getByTestId } = renderProvider();

    await waitFor(() => expect(getByTestId("probe").textContent).toBe("null"));
    expect(mockGetContext).not.toHaveBeenCalled();
  });
});
