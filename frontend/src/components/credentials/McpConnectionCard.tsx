/**
 * McpConnectionCard
 *
 * The one place on the credentials page that shows the MCP endpoint (#1836).
 * It sits above the API-key / custom-OAuth-app tabs because connecting is
 * what most visitors came for, and most of them need nothing from either tab:
 * Claude.ai, Claude Desktop, ChatGPT, Gemini, Muse, Cursor and Claude Code paste the bare
 * `…/mcp` URL and sign in with OAuth (they register themselves through
 * Dynamic Client Registration).
 *
 * Why the bare URL leads: an API key carries its own workspace, and an OAuth
 * token resolves it at login, so the `/mcp/w/<workspace-id>` path segment is
 * only ever needed to PIN an OAuth connector to one workspace. Without it an
 * OAuth connector follows the workspace selected in the Web UI — relevant
 * only when the user belongs to more than one, which is when the pinning
 * section is shown at all.
 *
 * "All tools" (#1609, #1849) puts `?profile=full` on every URL and command the
 * card renders and copies — the same switch the key-bearing snippets have —
 * so the Claude Code OAuth one-liner takes its full-profile form here.
 *
 * Key-bearing snippets (.mcp.json, ChatGPT Bearer, Codex CLI) stay on the
 * API-key tab next to the key they embed (MCPConfigBlock).
 */

"use client";

import { useId, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowRight, Check, ChevronDown, Copy } from "lucide-react";
import { CopyIconButton } from "@/components/common/CopyIconButton";
import { ChatGptAppLink } from "@/components/common/ChatGptAppLink";
import { PluginGuideLink } from "@/components/common/PluginGuideLink";
import { Section } from "@/components/common/Section";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { McpToolPicker } from "@/components/credentials/McpToolPicker";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import { useToast } from "@/hooks/use-toast";
import {
  buildClaudeOAuthCommand,
  mcpEndpoints,
  withFullProfile,
  withAdditionalTools,
} from "@/lib/mcp/url";

const TYPESCRIPT_SDK_URL = "https://github.com/kagura-ai/kagura-memory-typescript-sdk";
const PYTHON_SDK_URL = "https://github.com/kagura-ai/kagura-memory-python-sdk";
const MCP_CLIENTS_DOCS_URL =
  "https://github.com/kagura-ai/memory-cloud/blob/main/docs/mcp-clients.md";

export interface McpConnectionSettings {
  allTools: boolean;
  pinToWorkspace: boolean;
  additionalTools: string[];
}

export const DEFAULT_MCP_SETTINGS: McpConnectionSettings = {
  allTools: false,
  pinToWorkspace: false,
  additionalTools: [],
};

interface McpConnectionCardProps {
  variant?: "full" | "summary";
  title?: string;
  description?: string;
  settings?: McpConnectionSettings;
  onSettingsChange?: (settings: McpConnectionSettings) => void;
}

export function McpConnectionCard({ variant = "full", title, description, settings, onSettingsChange }: McpConnectionCardProps) {
  const t = useTranslations("mcpConnection");
  const tApiKeys = useTranslations("apiKeys");
  const tCommon = useTranslations("common");
  const { currentWorkspaceId } = useWorkspace();
  const { toast } = useToast();
  const { isCopied, copyToTarget } = useCopyFeedback();

  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080";
  const { baseUrl, mcpUrl, pinnedUrl } = useMemo(
    () => mcpEndpoints(apiUrl, currentWorkspaceId),
    [apiUrl, currentWorkspaceId],
  );

  // Not persisted, like MCPConfigBlock's switch: the bare URL (the core
  // profile, #1849) is the default. One derived URL feeds every display and
  // copy below.
  const [localSettings, setLocalSettings] = useState(DEFAULT_MCP_SETTINGS);
  const selectedSettings = settings ?? localSettings;
  const { allTools, pinToWorkspace, additionalTools } = selectedSettings;
  const updateSettings = (change: Partial<McpConnectionSettings>) => {
    const next = { ...selectedSettings, ...change };
    if (onSettingsChange) onSettingsChange(next);
    else setLocalSettings(next);
  };
  const pinSwitchId = useId();
  const summaryTitleId = useId();
  const allToolsSwitchId = useId();
  const allToolsHelpId = useId();
  const selectedUrl = pinToWorkspace && pinnedUrl ? pinnedUrl : mcpUrl;
  const shownUrl = allTools ? withFullProfile(selectedUrl) : withAdditionalTools(selectedUrl, additionalTools.join(","));
  const claudeCodeCommand = buildClaudeOAuthCommand(shownUrl);

  const shownPinnedUrl = pinnedUrl
    ? allTools ? withFullProfile(pinnedUrl) : withAdditionalTools(pinnedUrl, additionalTools.join(","))
    : null;

  const handleCopy = async (text: string, key: string) => {
    try {
      await copyToTarget(text, key);
    } catch {
      // Clipboard failure is a user-action failure → destructive toast with
      // the i18n'd "select it and copy manually" hint (copyText has already
      // tried the execCommand fallback, #987); the raw DOM exception text
      // would be English in every locale.
      toast({
        title: tCommon("error"),
        description: tCommon("copyFailedManualHint"),
        variant: "destructive",
      });
    }
  };

  if (variant === "summary") {
    return (
      <section
        id="mcp-connection"
        tabIndex={-1}
        aria-labelledby={summaryTitleId}
        className="mb-6 rounded-xl border border-blue-200 bg-blue-50 p-5 shadow-sm dark:border-blue-800 dark:bg-slate-800 sm:p-6"
      >
        <h2 id={summaryTitleId} className="text-xl font-semibold text-slate-900 dark:text-white">
          {title ?? t("quickStart.title")}
        </h2>
        <p className="mt-2 text-sm leading-6 text-slate-600 dark:text-slate-300">
          {description ?? t("oauthHint")}
        </p>
        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-stretch">
          <code className="min-w-0 flex-1 select-all break-all rounded-lg border border-blue-200 bg-white px-4 py-3 font-mono text-base leading-7 text-blue-900 dark:border-slate-600 dark:bg-slate-900 dark:text-blue-100">
            {shownUrl}
          </code>
          <Button
            type="button"
            onClick={() => handleCopy(shownUrl, "mcp-url")}
            className="min-h-12 shrink-0 gap-2 bg-blue-600 px-5 text-white hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            {isCopied("mcp-url") ? <Check aria-hidden="true" className="h-4 w-4" /> : <Copy aria-hidden="true" className="h-4 w-4" />}
            <span aria-live="polite">
              {isCopied("mcp-url") ? t("quickStart.copied") : t("copyUrl")}
            </span>
          </Button>
        </div>

        <Collapsible className="mt-3">
          <CollapsibleTrigger asChild>
            <Button variant="ghost" className="group min-h-11 gap-2 px-0">
              {t("quickStart.toolSettings")}
              <ChevronDown aria-hidden="true" className="h-4 w-4 transition-transform group-data-[state=open]:rotate-180" />
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent className="space-y-3 rounded-lg border border-blue-200 p-4 dark:border-slate-600">
            <div className="flex items-center gap-2">
              <Switch id={pinSwitchId} checked={pinToWorkspace} onCheckedChange={(checked) => updateSettings({ pinToWorkspace: checked })} disabled={!pinnedUrl} />
              <Label htmlFor={pinSwitchId}>{t("quickStart.pinWorkspace")}</Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch id={allToolsSwitchId} checked={allTools} onCheckedChange={(checked) => updateSettings({ allTools: checked })} />
              <Label htmlFor={allToolsSwitchId}>{t("quickStart.allTools")}</Label>
            </div>
            <div className="space-y-2">
              <p className="text-sm font-medium">{t("quickStart.additionalTools")}</p>
              <McpToolPicker value={additionalTools} onChange={(tools) => updateSettings({ additionalTools: tools })} disabled={allTools} />
              <p id={allToolsHelpId} className="text-xs leading-5 text-slate-600 dark:text-slate-300">
                {t(allTools ? "quickStart.fullHint" : "quickStart.additionalHint")}
              </p>
            </div>
            <p className="text-xs leading-5 text-slate-600 dark:text-slate-300">
              {t("quickStart.reconnect")}
            </p>
          </CollapsibleContent>
        </Collapsible>

        <div className="mt-3 space-y-2">
          {shownPinnedUrl && (
            <p className="text-xs leading-5 text-slate-600 dark:text-slate-300">
              {t(pinToWorkspace ? "pinHint" : "followsWorkspace")}
            </p>
          )}
          <a
            href={MCP_CLIENTS_DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex min-h-11 items-center gap-1 text-sm font-medium text-blue-700 underline underline-offset-4 hover:text-blue-900 dark:text-blue-300 dark:hover:text-blue-100"
          >
            {t("links.mcpClients")}
            <ArrowRight aria-hidden="true" className="h-4 w-4" />
          </a>
          <ChatGptAppLink />
          <PluginGuideLink />
        </div>
      </section>
    );
  }

  return (
    <Section title={`🔗 ${t("title")}`} description={t("description")}>
      <ChatGptAppLink />
      <PluginGuideLink />
      <div className="space-y-4">
        {/* The endpoint every client uses */}
        <div className="flex items-center gap-2">
          <code className="flex-1 bg-blue-50 dark:bg-blue-900/30 px-4 py-3 rounded border border-blue-200 dark:border-blue-800 text-sm font-mono text-blue-800 dark:text-blue-200 break-all">
            {shownUrl}
          </code>
          <CopyIconButton
            value={shownUrl}
            copyKey="mcp-url"
            label={t("copyUrl")}
            isCopied={isCopied}
            onCopy={handleCopy}
            className="text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-800"
          />
        </div>
        <p className="text-sm text-gray-700 dark:text-gray-300">
          {t("oauthHint")}
        </p>

        {/* All tools (#1609, #1849) — applies to every URL on the card */}
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Switch
              id={allToolsSwitchId}
              checked={allTools}
              onCheckedChange={(checked) => updateSettings({ allTools: checked })}
              aria-describedby={allToolsHelpId}
            />
            <Label htmlFor={allToolsSwitchId}>
              {tApiKeys("allToolsLabel")}
            </Label>
          </div>
          <p
            id={allToolsHelpId}
            className="text-xs text-gray-600 dark:text-gray-400"
          >
            {tApiKeys("allToolsHelp")}
          </p>
        </div>

        {/* Claude Code: one command, no key */}
        <div>
          <h4 className="text-sm font-medium text-gray-900 dark:text-gray-100 mb-2">
            {t("claudeCodeHeading")}
          </h4>
          <div className="relative">
            <pre className="bg-gray-900 text-gray-100 p-3 pr-12 rounded overflow-x-auto text-xs whitespace-pre-wrap break-all">
              {claudeCodeCommand}
            </pre>
            <div className="absolute top-2 right-2">
              <CopyIconButton
                value={claudeCodeCommand}
                copyKey="claude-code-command"
                label={t("copyCommand")}
                isCopied={isCopied}
                onCopy={handleCopy}
                className="text-gray-300 hover:text-white hover:bg-gray-700/50"
              />
            </div>
          </div>
          <p className="text-xs text-blue-700 dark:text-blue-300 mt-2">
            💡 {t("claudeCodeHint")}
          </p>
        </div>

        <p className="text-xs text-gray-600 dark:text-gray-400">
          {t("apiKeyHint")}
        </p>

        {/* Workspace-pinned endpoint follows the same tool selection. */}
        {shownPinnedUrl && (
          <div className="space-y-2 border-t border-gray-200 pt-3 dark:border-gray-700">
            <h3 className="text-sm font-medium">{t("pinToggle")}</h3>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 select-all break-all rounded border border-gray-200 bg-gray-50 px-3 py-2 font-mono text-xs dark:border-gray-700 dark:bg-gray-800">
                {shownPinnedUrl}
              </code>
              <CopyIconButton
                value={shownPinnedUrl}
                copyKey="pinned-mcp-url"
                label={t("copyPinnedUrl")}
                isCopied={isCopied}
                onCopy={handleCopy}
              />
            </div>
            <p className="text-xs text-gray-600 dark:text-gray-400">{t("pinHint")}</p>
          </div>
        )}

        {/* Reference links */}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          <a
            href={MCP_CLIENTS_DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary underline hover:text-primary/80"
          >
            {t("links.mcpClients")}
          </a>
          <a
            href={TYPESCRIPT_SDK_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary underline hover:text-primary/80"
          >
            {t("links.typescriptSdk")}
          </a>
          <a
            href={PYTHON_SDK_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary underline hover:text-primary/80"
          >
            {t("links.pythonSdk")}
          </a>
          <a
            href={`${baseUrl}/redoc`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-primary underline hover:text-primary/80"
          >
            {t("links.restApi")}
          </a>
        </div>
      </div>
    </Section>
  );
}

export function McpReferenceLinks() {
  const t = useTranslations("mcpConnection");
  const { baseUrl } = mcpEndpoints(process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080", null);
  const links = [
    { href: MCP_CLIENTS_DOCS_URL, label: "mcpClients" },
    { href: TYPESCRIPT_SDK_URL, label: "typescriptSdk" },
    { href: PYTHON_SDK_URL, label: "pythonSdk" },
    { href: baseUrl + "/redoc", label: "restApi" },
  ];
  return (
    <nav aria-label={t("links.label")} className="mt-4 flex flex-wrap gap-x-4 gap-y-2 text-sm">
      {links.map(link => (
        <a key={link.label} href={link.href} target="_blank" rel="noopener noreferrer" className="inline-flex min-h-10 items-center text-primary underline underline-offset-4 hover:text-primary/80">
          {t(`links.${link.label}`)}
        </a>
      ))}
    </nav>
  );
}
