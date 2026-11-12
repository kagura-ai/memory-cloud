import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: (ns: string) => (k: string) => `${ns}.${k}`,
}));

import ja from "@/messages/ja.json";
import { AnnouncementEmailsOptIn } from "./AnnouncementEmailsOptIn";

/** #2761: the sign-up opt-in is its own checkbox with its own label. */
describe("AnnouncementEmailsOptIn", () => {
  it("renders the opt-in copy and reflects `checked`", () => {
    render(
      <AnnouncementEmailsOptIn
        checked={false}
        onCheckedChange={() => {}}
        testId="optin"
      />,
    );
    const box = screen.getByRole("checkbox", {
      name: "announcementEmails.optIn",
    }) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(box).toHaveAttribute("data-testid", "optin");
  });

  it("reports changes", () => {
    const onChange = vi.fn();
    render(
      <AnnouncementEmailsOptIn checked={false} onCheckedChange={onChange} />,
    );
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("uses the catalog's full-width parentheses in Japanese", () => {
    expect(ja.announcementEmails.optIn).toContain("（任意）");
    expect(ja.announcementEmails.optIn).not.toMatch(/[()]/);
  });
});
