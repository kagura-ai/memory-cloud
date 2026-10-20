/**
 * Tests for DefaultContextControl and defaultContextLabel (#2129).
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  DefaultContextControl,
  DefaultContextName,
} from "./DefaultContextControl";
import { defaultContextLabel } from "@/hooks/useWorkspaceContextOptions";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));

vi.mock("@/contexts/WorkspaceContext", () => ({
  useWorkspace: () => ({ currentWorkspaceId: "ws-1" }),
}));

const W1 = [{ id: "ctx-a", label: "Alpha" }];
const W2 = [{ id: "ctx-b", label: "Beta" }];

describe("DefaultContextControl", () => {
  it("drops a pending choice that is no longer offered", () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <DefaultContextControl
        currentId={null}
        currentName={null}
        options={W1}
        onSave={onSave}
        ariaLabel="pick"
      />,
    );
    fireEvent.change(screen.getByLabelText("pick"), {
      target: { value: "ctx-a" },
    });
    expect(screen.getByText("save")).toBeInTheDocument();

    // Workspace switch: the pending choice is not among the new options.
    rerender(
      <DefaultContextControl
        currentId={null}
        currentName={null}
        options={W2}
        onSave={onSave}
        ariaLabel="pick"
      />,
    );
    expect((screen.getByLabelText("pick") as HTMLSelectElement).value).toBe("");
    expect(screen.queryByText("save")).not.toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
  });

  it("saves the shown choice", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <DefaultContextControl
        currentId={null}
        currentName={null}
        options={W1}
        onSave={onSave}
        ariaLabel="pick"
      />,
    );
    fireEvent.change(screen.getByLabelText("pick"), {
      target: { value: "ctx-a" },
    });
    fireEvent.click(screen.getByText("save"));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("ctx-a"));
  });
});

describe("DefaultContextName", () => {
  it("announces 'none' to screen readers for no default", () => {
    render(<DefaultContextName currentId={null} currentName={null} />);
    expect(screen.getByText("none")).toHaveClass("sr-only");
    expect(screen.getByText("—")).toHaveAttribute("aria-hidden", "true");
  });

  it("flags a default the owner can no longer read", () => {
    render(<DefaultContextName currentId="ctx-x" currentName={null} />);
    expect(screen.getByText("unavailable")).toBeInTheDocument();
  });
});

describe("defaultContextLabel", () => {
  it("prefers the offered label (display name) over the server name", () => {
    expect(defaultContextLabel(W1, "ctx-a", "alpha")).toBe("Alpha");
  });

  it("falls back to the server name outside the offered list", () => {
    expect(defaultContextLabel(W1, "ctx-z", "zeta")).toBe("zeta");
  });

  it("is null without a default", () => {
    expect(defaultContextLabel(W1, null, "alpha")).toBeNull();
  });
});
