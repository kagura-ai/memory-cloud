/**
 * Alert variants (#1646).
 *
 * The `upsell` and `warning` variants replace hand-rolled gate notices that
 * carried light-mode colours only, so on a dark background they rendered
 * dark text on a dark card. The regression that shipped was a missing `dark:`
 * token, which is exactly what a class-string check can see: every colour a
 * variant sets for light mode (border, background, text, icon) must have a
 * dark-mode counterpart.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Alert, AlertDescription, AlertTitle } from "./alert";

function classesOf(variant: "upsell" | "warning"): string[] {
  const { getByRole } = render(
    <Alert variant={variant}>
      <AlertTitle>Title</AlertTitle>
      <AlertDescription>Body</AlertDescription>
    </Alert>,
  );
  return getByRole("alert").className.split(/\s+/);
}

describe("Alert", () => {
  it.each([
    ["upsell", "purple"],
    ["warning", "amber"],
  ] as const)(
    "%s variant carries light and dark tokens for border, background, text and icon",
    (variant, hue) => {
      const classes = classesOf(variant);

      // Light mode.
      expect(classes).toContain(`bg-${hue}-50`);
      expect(classes).toContain(`text-${hue}-900`);
      expect(classes).toContain(`border-${hue}-300/70`);
      expect(classes).toContain(`[&>svg]:text-${hue}-600`);

      // Dark mode: each of the four has its own token.
      expect(classes).toContain(`dark:bg-${hue}-950/40`);
      expect(classes).toContain(`dark:text-${hue}-100`);
      expect(classes).toContain(`dark:border-${hue}-800`);
      expect(classes).toContain(`dark:[&>svg]:text-${hue}-300`);

      // The variant's colours replace the default ones instead of racing them.
      expect(classes).not.toContain("bg-background");
      expect(classes).not.toContain("text-foreground");
      expect(classes).not.toContain("[&>svg]:text-foreground");
    },
  );

  it("keeps the default variant on the theme tokens", () => {
    const { getByRole } = render(<Alert>Body</Alert>);
    const classes = getByRole("alert").className.split(/\s+/);
    expect(classes).toContain("bg-background");
    expect(classes).toContain("text-foreground");
  });
});
