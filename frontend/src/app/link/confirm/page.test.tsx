/**
 * /link/confirm (#2786): the token is read once and stripped from the URL,
 * the preview names the provider account, only the browser that started the
 * sign-in is offered "Connect", "This wasn't me" discards the link, and every
 * answer of the backend has its own state.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

vi.mock("next-intl", () => ({
  useTranslations: () => (k: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length > 0 ? `${k}:${JSON.stringify(vars)}` : k,
}));

let mockToken: string | null = "tok_SECRET_link";
const mockReplace = vi.fn();
vi.mock("next/navigation", () => ({
  useSearchParams: () => ({
    get: (k: string) => (k === "token" ? mockToken : null),
  }),
  useRouter: () => ({ replace: mockReplace }),
  usePathname: () => "/link/confirm",
}));

const mockToast = vi.fn();
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => ({ toast: mockToast }),
}));

vi.mock("@/components/common/LoadingState", async () => {
  const actual = await vi.importActual<
    typeof import("@/components/common/LoadingState")
  >("@/components/common/LoadingState");
  return {
    ...actual,
    SpinnerLoading: ({ message }: { message?: string }) => (
      <div role="status">{message}</div>
    ),
  };
});

const { mockPreview, mockComplete, mockCancel, FakeApiError } = vi.hoisted(
  () => {
    class FakeApiError extends Error {
      readonly status: number;
      readonly error?: string;
      constructor(status: number, error?: string) {
        super("fake");
        this.status = status;
        this.error = error;
      }
    }
    return {
      mockPreview: vi.fn(),
      mockComplete: vi.fn(),
      mockCancel: vi.fn(),
      FakeApiError,
    };
  },
);
vi.mock("@/lib/auth/providerLinkConfirm", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/auth/providerLinkConfirm")
  >("@/lib/auth/providerLinkConfirm");
  return {
    ...actual,
    previewProviderLink: (...a: unknown[]) => mockPreview(...a),
    completeProviderLink: (...a: unknown[]) => mockComplete(...a),
    cancelProviderLink: (...a: unknown[]) => mockCancel(...a),
  };
});
vi.mock("@/lib/api/base", () => ({ ApiError: FakeApiError }));

import ProviderLinkConfirmPage from "./page";

const TOKEN = "tok_SECRET_link";
const ORIGIN = "http://localhost:3000";
const originalLocation = window.location;
let hrefAssignments: string[] = [];
const consoleSpies = (["log", "info", "warn", "error", "debug"] as const).map(
  (level) => vi.spyOn(console, level),
);

const GITHUB = JSON.stringify({ provider: "providerGithub" });
const GITHUB_AS_OCTO = JSON.stringify({
  provider: "providerGithub",
  login: "octo-cat",
});

function preview(overrides: Record<string, unknown> = {}) {
  return {
    provider: "github",
    provider_login: "octo-cat",
    provider_name: "Octo Cat",
    provider_avatar_url: "https://avatars.githubusercontent.com/u/1?v=4",
    expires_at: "2030-01-01T00:15:00Z",
    same_browser: true,
    ...overrides,
  };
}

async function renderConfirm() {
  render(<ProviderLinkConfirmPage />);
  await screen.findByText(`title:${GITHUB}`);
}

const connectButton = () => screen.queryByRole("button", { name: "connect" });
const notMeButton = () => screen.getByRole("button", { name: "notMe" });

beforeEach(() => {
  vi.clearAllMocks();
  mockToken = TOKEN;
  hrefAssignments = [];
  mockPreview.mockResolvedValue(preview());
  mockComplete.mockResolvedValue({ redirect_url: "/workspace/dashboard" });
  mockCancel.mockResolvedValue(undefined);
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get origin() {
        return ORIGIN;
      },
      get href() {
        return `${ORIGIN}/link/confirm`;
      },
      set href(val: string) {
        hrefAssignments.push(val);
      },
    },
  });
});

afterEach(() => {
  Object.defineProperty(window, "location", {
    configurable: true,
    value: originalLocation,
  });
  // The token never reaches the console, and never the page.
  for (const spy of consoleSpies) {
    expect(JSON.stringify(spy.mock.calls)).not.toContain("tok_SECRET");
  }
  expect(document.body.innerHTML).not.toContain("tok_SECRET");
});

describe("/link/confirm — the link", () => {
  it("reads the token once, strips it from the URL and previews it", async () => {
    await renderConfirm();
    expect(mockReplace).toHaveBeenCalledWith("/link/confirm", {
      scroll: false,
    });
    expect(mockPreview).toHaveBeenCalledTimes(1);
    expect(mockPreview).toHaveBeenCalledWith(TOKEN);
  });

  it("shows a loading state while the preview is pending", () => {
    mockPreview.mockReturnValue(new Promise(() => {}));
    render(<ProviderLinkConfirmPage />);
    expect(screen.getByRole("status")).toHaveTextContent("loading");
  });

  it("without a token → invalid, no call", async () => {
    mockToken = null;
    render(<ProviderLinkConfirmPage />);
    expect(await screen.findByText("invalidTitle")).toBeVisible();
    expect(screen.getByText("invalidMessage")).toBeVisible();
    expect(mockPreview).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it.each([400, 404, 422])("preview %s → invalid", async (status) => {
    mockPreview.mockRejectedValue(new FakeApiError(status));
    render(<ProviderLinkConfirmPage />);
    expect(await screen.findByText("invalidTitle")).toBeVisible();
    expect(connectButton()).toBeNull();
  });

  it("preview 429 → rate limited", async () => {
    mockPreview.mockRejectedValue(new FakeApiError(429, "RATE-001"));
    render(<ProviderLinkConfirmPage />);
    expect(await screen.findByText("rateLimitedTitle")).toBeVisible();
    expect(connectButton()).toBeNull();
  });

  it.each([500, 503, 0])(
    "preview %s → retryable error, and Retry previews again",
    async (status) => {
      mockPreview.mockRejectedValueOnce(new FakeApiError(status));
      render(<ProviderLinkConfirmPage />);
      fireEvent.click(await screen.findByRole("button", { name: "retry" }));
      await screen.findByText(`title:${GITHUB}`);
      expect(mockPreview).toHaveBeenCalledTimes(2);
    },
  );
});

describe("/link/confirm — the browser that started the sign-in", () => {
  it("names the provider account first and states the consequence", async () => {
    await renderConfirm();
    expect(screen.getByText("octo-cat")).toBeVisible();
    expect(screen.getByText("Octo Cat")).toBeVisible();
    expect(screen.getByText(`consequence:${GITHUB}`)).toBeVisible();
    expect(connectButton()).toBeVisible();
    expect(notMeButton()).toBeVisible();
  });

  it("connects, then follows redirect_url", async () => {
    mockComplete.mockResolvedValue({ redirect_url: "/oauth/authorize?x=1" });
    await renderConfirm();
    fireEvent.click(connectButton()!);

    await waitFor(() =>
      expect(hrefAssignments).toEqual(["/oauth/authorize?x=1"]),
    );
    expect(mockComplete).toHaveBeenCalledWith(TOKEN);
    expect(mockCancel).not.toHaveBeenCalled();
  });

  it("disables both buttons while connecting", async () => {
    mockComplete.mockReturnValue(new Promise(() => {}));
    await renderConfirm();
    fireEvent.click(connectButton()!);

    const connecting = await screen.findByRole("button", {
      name: "connecting",
    });
    expect(connecting).toBeDisabled();
    expect(notMeButton()).toBeDisabled();
  });

  it("shows the avatar of the provider's own host", async () => {
    await renderConfirm();
    const img = document.querySelector("img");
    expect(img?.getAttribute("src")).toBe(
      "https://avatars.githubusercontent.com/u/1?v=4",
    );
    expect(img?.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it.each([
    "https://evil.example.com/a.png",
    "http://avatars.githubusercontent.com/u/1",
    "https://lh3.googleusercontent.com/a/abc",
    null,
  ])("shows an initial instead of the avatar %s", async (url) => {
    mockPreview.mockResolvedValue(preview({ provider_avatar_url: url }));
    await renderConfirm();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText("O")).toBeVisible();
  });

  it("names Google accounts by their address", async () => {
    mockPreview.mockResolvedValue(
      preview({
        provider: "google",
        provider_login: "someone@example.com",
        provider_name: null,
        provider_avatar_url: "https://lh3.googleusercontent.com/a/abc",
      }),
    );
    render(<ProviderLinkConfirmPage />);
    await screen.findByText(
      `title:${JSON.stringify({ provider: "providerGoogle" })}`,
    );
    expect(screen.getByText("someone@example.com")).toBeVisible();
    expect(document.querySelector("img")?.getAttribute("src")).toBe(
      "https://lh3.googleusercontent.com/a/abc",
    );
  });

  it.each([400, 404, 422])("complete %s → invalid", async (status) => {
    mockComplete.mockRejectedValue(new FakeApiError(status, "AUTH-301"));
    await renderConfirm();
    fireEvent.click(connectButton()!);
    expect(await screen.findByText("invalidTitle")).toBeVisible();
    expect(connectButton()).toBeNull();
    expect(hrefAssignments).toEqual([]);
  });

  it("complete 403 → the other-browser state, without a connect button", async () => {
    mockComplete.mockRejectedValue(new FakeApiError(403, "AUTH-310"));
    await renderConfirm();
    fireEvent.click(connectButton()!);
    expect(
      await screen.findByText(`otherBrowserMessage:${GITHUB_AS_OCTO}`),
    ).toBeVisible();
    expect(connectButton()).toBeNull();
    expect(notMeButton()).toBeVisible();
  });

  it("complete 429 → rate limited", async () => {
    mockComplete.mockRejectedValue(new FakeApiError(429, "RATE-001"));
    await renderConfirm();
    fireEvent.click(connectButton()!);
    expect(await screen.findByText("rateLimitedTitle")).toBeVisible();
  });

  it("complete 503 → connected, sign in again", async () => {
    mockComplete.mockRejectedValue(new FakeApiError(503, "HTTP-503"));
    await renderConfirm();
    fireEvent.click(connectButton()!);
    expect(await screen.findByText(`connectedTitle:${GITHUB}`)).toBeVisible();
    expect(screen.getByText(`connectedMessage:${GITHUB}`)).toBeVisible();
    expect(screen.getByRole("link", { name: "signIn" })).toHaveAttribute(
      "href",
      "/login",
    );
    expect(hrefAssignments).toEqual([]);
  });

  it("a network failure on complete → a toast, the buttons come back", async () => {
    mockComplete.mockRejectedValue(new FakeApiError(0));
    await renderConfirm();
    fireEvent.click(connectButton()!);
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({
          description: "genericError",
          variant: "destructive",
        }),
      ),
    );
    expect(connectButton()).toBeEnabled();
  });
});

describe("/link/confirm — another browser", () => {
  it("offers no connect button, says where to open it, and keeps This wasn't me", async () => {
    mockPreview.mockResolvedValue(preview({ same_browser: false }));
    render(<ProviderLinkConfirmPage />);
    expect(
      await screen.findByText(`otherBrowserMessage:${GITHUB_AS_OCTO}`),
    ).toBeVisible();
    expect(screen.getByText(`alwaysWorks:${GITHUB}`)).toBeVisible();
    expect(connectButton()).toBeNull();
    expect(notMeButton()).toBeVisible();
    expect(mockComplete).not.toHaveBeenCalled();
  });
});

describe("/link/confirm — This wasn't me", () => {
  it.each([true, false])(
    "cancels the link (same_browser %s) and says nothing was connected",
    async (sameBrowser) => {
      mockPreview.mockResolvedValue(preview({ same_browser: sameBrowser }));
      render(<ProviderLinkConfirmPage />);
      fireEvent.click(await screen.findByRole("button", { name: "notMe" }));

      expect(await screen.findByText("cancelledTitle")).toBeVisible();
      expect(screen.getByText("cancelledMessage")).toBeVisible();
      expect(screen.getByText(`alwaysWorks:${GITHUB}`)).toBeVisible();
      expect(mockCancel).toHaveBeenCalledWith(TOKEN);
      expect(mockComplete).not.toHaveBeenCalled();
      expect(connectButton()).toBeNull();
    },
  );

  it("cancel 429 → rate limited", async () => {
    mockCancel.mockRejectedValue(new FakeApiError(429, "RATE-001"));
    await renderConfirm();
    fireEvent.click(notMeButton());
    expect(await screen.findByText("rateLimitedTitle")).toBeVisible();
  });

  it("a network failure on cancel → a toast, the page stays", async () => {
    mockCancel.mockRejectedValue(new FakeApiError(0));
    await renderConfirm();
    fireEvent.click(notMeButton());
    await waitFor(() => expect(mockToast).toHaveBeenCalled());
    expect(notMeButton()).toBeEnabled();
    expect(screen.queryByText("cancelledTitle")).toBeNull();
  });
});
