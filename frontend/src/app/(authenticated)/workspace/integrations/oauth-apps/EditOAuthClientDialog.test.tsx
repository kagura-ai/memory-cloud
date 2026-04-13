/**
 * Tests for EditOAuthClientDialog (Issue #219).
 *
 * Covers:
 *   - Pre-fill from existing client on open
 *   - Successful save: calls API, shows toast, triggers onSuccess
 *   - Client-side validation: empty name, empty URIs
 *   - API error (422): field-level errors displayed, dialog stays open
 *   - Add / remove redirect URI interactions
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";

import { EditOAuthClientDialog } from "./EditOAuthClientDialog";
import { ApiError } from "@/lib/api/base";
import type { OAuth2Client } from "@/lib/api/oauth";

// ---------- Mocks ------------------------------------------------------------

const mockUpdateOAuth2Client = vi.fn();
const mockToast = vi.fn();

vi.mock("@/lib/api/oauth", () => ({
  updateOAuth2Client: (...args: unknown[]) => mockUpdateOAuth2Client(...args),
}));

const stableToastCtx = { toast: mockToast };
vi.mock("@/hooks/use-toast", () => ({
  useToast: () => stableToastCtx,
}));

const stableTranslator = (key: string) => key;
vi.mock("next-intl", () => ({
  useTranslations: (_namespace: string) => stableTranslator,
}));

// ---------- Helpers ----------------------------------------------------------

const MOCK_CLIENT: OAuth2Client = {
  id: 1,
  client_id: "test-client-id",
  client_name: "Test Client",
  redirect_uris: ["https://example.com/callback"],
  grant_types: ["authorization_code"],
  response_types: ["code"],
  scope: "openid",
  token_endpoint_auth_method: "client_secret_post",
  provider: "custom",
  created_at: "2026-04-01T00:00:00Z",
  plaintext_secret: null,
  is_visible: false,
  visibility_expires_at: null,
};

function renderDialog(
  overrides: Partial<{
    client: OAuth2Client | null;
    open: boolean;
    onOpenChange: (open: boolean) => void;
    onSuccess: () => void;
  }> = {},
) {
  const props = {
    client: MOCK_CLIENT,
    open: true,
    onOpenChange: vi.fn(),
    onSuccess: vi.fn(),
    ...overrides,
  };
  const utils = render(<EditOAuthClientDialog {...props} />);
  return { ...utils, props };
}

// ---------- Tests ------------------------------------------------------------

describe("EditOAuthClientDialog", () => {
  beforeEach(() => {
    mockUpdateOAuth2Client.mockReset();
    mockToast.mockReset();
  });

  it("pre-fills client name and redirect URIs from the client prop", () => {
    renderDialog();

    const nameInput = screen.getByPlaceholderText("appNamePlaceholder");
    expect(nameInput).toHaveValue("Test Client");

    const uriInput = screen.getByPlaceholderText("redirectUriPlaceholder");
    expect(uriInput).toHaveValue("https://example.com/callback");
  });

  it("calls updateOAuth2Client with trimmed values on save", async () => {
    mockUpdateOAuth2Client.mockResolvedValueOnce({});
    const { props } = renderDialog();

    const saveButton = screen.getByRole("button", { name: "saveChanges" });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(mockUpdateOAuth2Client).toHaveBeenCalledWith("test-client-id", {
        client_name: "Test Client",
        redirect_uris: ["https://example.com/callback"],
      });
    });

    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "success" }),
    );
    expect(props.onSuccess).toHaveBeenCalled();
  });

  it("shows validation error when client name is empty", async () => {
    renderDialog();

    const nameInput = screen.getByPlaceholderText("appNamePlaceholder");
    fireEvent.change(nameInput, { target: { value: "   " } });

    const saveButton = screen.getByRole("button", { name: "saveChanges" });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByText("appNameRequired")).toBeInTheDocument();
    });

    expect(mockUpdateOAuth2Client).not.toHaveBeenCalled();
  });

  it("shows validation error when all redirect URIs are empty", async () => {
    renderDialog();

    const uriInput = screen.getByPlaceholderText("redirectUriPlaceholder");
    fireEvent.change(uriInput, { target: { value: "" } });

    const saveButton = screen.getByRole("button", { name: "saveChanges" });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByText("redirectUriRequired")).toBeInTheDocument();
    });

    expect(mockUpdateOAuth2Client).not.toHaveBeenCalled();
  });

  it("displays API error and keeps the dialog open on failure", async () => {
    mockUpdateOAuth2Client.mockRejectedValueOnce(
      new ApiError({
        status: 422,
        message: "Validation failed",
        details: {
          detail: [
            { loc: ["body", "redirect_uris"], msg: "Invalid URI format" },
          ],
        },
      }),
    );

    const { props } = renderDialog();

    const saveButton = screen.getByRole("button", { name: "saveChanges" });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByText("Invalid URI format")).toBeInTheDocument();
    });

    // Dialog stays open — onOpenChange(false) not called on error
    expect(props.onOpenChange).not.toHaveBeenCalledWith(false);
  });

  it("adds and removes redirect URI fields", () => {
    renderDialog();

    // Initially one URI field
    expect(
      screen.getAllByPlaceholderText("redirectUriPlaceholder"),
    ).toHaveLength(1);

    // Add a URI
    fireEvent.click(screen.getByText("+ addRedirectUri"));
    expect(
      screen.getAllByPlaceholderText("redirectUriPlaceholder"),
    ).toHaveLength(2);

    // Remove button appears when > 1 URI
    const removeButtons = screen.getAllByTitle("removeUri");
    expect(removeButtons).toHaveLength(2);

    // Remove one URI
    fireEvent.click(removeButtons[0]);
    expect(
      screen.getAllByPlaceholderText("redirectUriPlaceholder"),
    ).toHaveLength(1);
  });

  it("disables buttons while saving", async () => {
    let resolveUpdate: () => void = () => {};
    mockUpdateOAuth2Client.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        resolveUpdate = resolve;
      }),
    );

    renderDialog();

    const saveButton = screen.getByRole("button", { name: "saveChanges" });
    fireEvent.click(saveButton);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "saving" })).toBeDisabled();
    });

    // Resolve the save
    resolveUpdate();

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
  });
});
