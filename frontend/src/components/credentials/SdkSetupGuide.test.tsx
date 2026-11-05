import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SdkSetupGuide } from "./SdkSetupGuide";

vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
const copy = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
vi.mock("@/hooks/useCopyFeedback", () => ({ useCopyFeedback: () => ({ isCopied: () => false, copyToTarget: copy }) }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
afterEach(() => { vi.unstubAllEnvs(); copy.mockClear(); });

describe("SDK installation guidance", () => {
  it.each([
    ["api", "npm install kagura-memory", "pip install kagura-memory"],
    ["secrets", "npm install kagura-memory age-encryption", 'pip install "kagura-memory[secret]"'],
  ] as const)("shows the correct packages and copy action for %s", (kind, npm, pip) => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://example.test/api/v1");
    render(<SdkSetupGuide kind={kind} />);
    expect(screen.getByText(npm)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "copyInstall" }));
    expect(copy).toHaveBeenCalledWith(npm, "guide-code");
    expect(screen.getAllByText(/mcpUrl: "https:\/\/example.test\/mcp"/)).toHaveLength(kind === "secrets" ? 2 : 1);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Python" }), { button: 0, ctrlKey: false });
    expect(screen.getByText(pip)).toBeInTheDocument();
    expect(screen.getAllByText(/os.environ\["KAGURA_API_KEY"\]/)).toHaveLength(kind === "secrets" ? 2 : 1);
    fireEvent.click(screen.getByRole("button", { name: "copyInstall" }));
    expect(copy).toHaveBeenLastCalledWith(pip, "guide-code");
  });
});
