import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "@/messages/en.json";
import ja from "@/messages/ja.json";
import { ResourceSetupGuide } from "./ResourceSetupGuide";

vi.mock("@/contexts/WorkspaceContext", () => ({ useWorkspace: () => ({ currentWorkspace: { id: "sample-workspace" } }) }));
vi.mock("@/hooks/useCopyFeedback", () => ({ useCopyFeedback: () => ({ isCopied: () => false, copyToTarget: vi.fn() }) }));
vi.mock("@/hooks/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));
afterEach(() => vi.unstubAllEnvs());

describe("resource setup guidance", () => {
  it.each([["ja", ja], ["en", en]] as const)("provides ingestion and full URLs with sample prompts in %s", (locale, messages) => {
    vi.stubEnv("NEXT_PUBLIC_API_URL", "https://example.test/api/v1/");
    render(<NextIntlClientProvider locale={locale} messages={messages}><ResourceSetupGuide /></NextIntlClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: messages.resourceSetup.title }));
    expect(screen.getByText("https://example.test/mcp/w/sample-workspace?add_tools=setup_resource,ingest_events,get_resource_schema,get_resource_impact,list_resource_tokens")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("tab", { name: messages.resourceSetup.fullMode }), { button: 0, ctrlKey: false });
    expect(screen.getByText("https://example.test/mcp/w/sample-workspace?profile=full")).toBeInTheDocument();
    expect(screen.getByText(messages.resourceSetup.inspectPrompt)).toBeInTheDocument();
    expect(screen.getByText(messages.resourceSetup.createPrompt)).toBeInTheDocument();
    expect(screen.getByText(messages.resourceSetup.availability)).toBeInTheDocument();
  });
});
