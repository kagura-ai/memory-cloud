import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PluginGuideLink } from "./PluginGuideLink";

const { current } = vi.hoisted(() => ({ current: { locale: "en" } }));
vi.mock("@/i18n", () => ({ useLocale: () => current }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));

describe("PluginGuideLink", () => {
  it.each([ ["en", ""], ["ja", "/ja"], ["fr", ""] ])("opens the guide for %s in a separate tab", (locale, prefix) => {
    current.locale = locale;
    render(<PluginGuideLink />);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", `https://www.kagura-ai.com${prefix}/help/guide/#plugins`);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(link).toHaveAccessibleName("label newTab");
  });
});
