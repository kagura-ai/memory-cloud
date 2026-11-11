import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import { ChatGptAppLink, chatGptAppHref } from "./ChatGptAppLink";

const LISTING = "https://example.com/chatgpt-app";
let mockInfo: { chatgpt_app_url?: string | null } | null = null;
vi.mock("@/hooks/useSystemFeatures", () => ({ useSystemInfo: () => mockInfo }));

afterEach(cleanup);
beforeEach(() => {
  mockInfo = null;
});

function renderIn(locale: "en" | "ja") {
  const messages = locale === "ja" ? ja : en;
  return render(
    <NextIntlClientProvider locale={locale} messages={messages}>
      <ChatGptAppLink />
    </NextIntlClientProvider>,
  );
}

describe("chatGptAppHref (#2760)", () => {
  it.each([
    [LISTING, LISTING],
    [null, null],
    [undefined, null],
    ["", null],
    ["http://example.com/chatgpt-app", null],
    ["javascript:alert(1)", null],
    ["chatgpt-app", null],
    ["//example.com/chatgpt-app", null],
    ["https://chatgpt.example@example.com/chatgpt-app", null],
    ["https://user:pass@example.com/chatgpt-app", null],
  ])("turns %s into %s", (value, expected) => {
    expect(chatGptAppHref(value)).toBe(expected);
  });
});

describe("ChatGptAppLink (#2760)", () => {
  it.each([
    ["while system info loads", null],
    ["when the deployment sets no URL", { chatgpt_app_url: null }],
    ["on an older backend without the field", {}],
    ["when the API returns a non-https value", { chatgpt_app_url: "javascript:alert(1)" }],
    ["when the API returns an http value", { chatgpt_app_url: "http://example.com/chatgpt-app" }],
  ])("renders nothing %s", (_label, info) => {
    mockInfo = info;
    const { container } = renderIn("en");
    expect(container).toBeEmptyDOMElement();
  });

  it.each([
    ["en", en],
    ["ja", ja],
  ] as const)("links the configured listing in a new tab in %s", (locale, messages) => {
    mockInfo = { chatgpt_app_url: LISTING };
    renderIn(locale);
    const link = screen.getByRole("link", {
      name: `${messages.chatgptApp.label} ${messages.chatgptApp.newTab.trim()}`,
    });
    expect(link).toHaveAttribute("href", LISTING);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByText(messages.chatgptApp.description)).toBeInTheDocument();
  });
});
