/**
 * #1939: a workspace back on Free keeps its data, but its paid-only features
 * pause. The banner names each paused feature and the way back (re-subscribe).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SuspendedFeaturesBanner } from "./SuspendedFeaturesBanner";

vi.mock("next-intl", () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
  }: {
    href: string;
    children: React.ReactNode;
  }) => <a href={href}>{children}</a>,
}));

describe("SuspendedFeaturesBanner", () => {
  it("renders nothing when no feature is suspended", () => {
    const { container } = render(<SuspendedFeaturesBanner suspended={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("lists each suspended feature with the re-subscribe hint", () => {
    render(<SuspendedFeaturesBanner suspended={["connectors", "sleep"]} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("planPage.suspended.title")).toBeInTheDocument();
    expect(
      screen.getByText("planPage.suspended.features.connectors"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("planPage.suspended.features.sleep"),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("planPage.suspended.features.public"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("planPage.suspended.resume")).toBeInTheDocument();
  });

  it("ignores a feature name it has no copy for", () => {
    render(<SuspendedFeaturesBanner suspended={["teleport", "public"]} />);
    expect(
      screen.queryByText("planPage.suspended.features.teleport"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("planPage.suspended.features.public"),
    ).toBeInTheDocument();
  });

  it("links to the plan page only when asked", () => {
    const { rerender } = render(
      <SuspendedFeaturesBanner suspended={["resources"]} />,
    );
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    rerender(
      <SuspendedFeaturesBanner suspended={["resources"]} showPlanLink />,
    );
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "/workspace/settings/plan",
    );
  });
});
