import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { KaguraLogo } from "./KaguraLogo";

/** SVG <image> hrefs live in the `href` attribute (SVG 2) — read it directly. */
function wordmarkImages(container: HTMLElement) {
  return Array.from(container.querySelectorAll("image")).filter((el) =>
    (el.getAttribute("href") ?? "").includes("wordmark"),
  );
}

describe("KaguraLogo image variant", () => {
  it('surface="auto" renders BOTH wordmarks, toggled by dark mode', () => {
    const { container } = render(<KaguraLogo variant="image" surface="auto" />);
    const wordmarks = wordmarkImages(container);
    expect(wordmarks).toHaveLength(2);

    const dark = wordmarks.find((el) =>
      (el.getAttribute("href") ?? "").includes("wordmark-light"),
    );
    const lightInk = wordmarks.find(
      (el) => !(el.getAttribute("href") ?? "").includes("wordmark-light"),
    );

    // Dark-ink wordmark shows in light mode, hides in dark mode.
    expect(lightInk?.getAttribute("class")).toContain("dark:hidden");
    // White wordmark hidden in light mode, shown in dark mode.
    expect(dark?.getAttribute("class")).toContain("hidden");
    expect(dark?.getAttribute("class")).toContain("dark:block");
  });

  it('surface="dark" renders only the white wordmark', () => {
    const { container } = render(<KaguraLogo variant="image" surface="dark" />);
    const wordmarks = wordmarkImages(container);
    expect(wordmarks).toHaveLength(1);
    expect(wordmarks[0].getAttribute("href")).toContain("wordmark-light");
  });

  it('surface="light" (default) renders only the dark-ink wordmark', () => {
    const { container } = render(<KaguraLogo variant="image" />);
    const wordmarks = wordmarkImages(container);
    expect(wordmarks).toHaveLength(1);
    expect(wordmarks[0].getAttribute("href")).toContain("kagura-wordmark.png");
    expect(wordmarks[0].getAttribute("href")).not.toContain("wordmark-light");
  });
});
