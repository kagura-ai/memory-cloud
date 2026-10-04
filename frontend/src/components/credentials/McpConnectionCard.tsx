/**
 * McpConnectionCard
 *
 * The one place on the credentials page that shows the MCP endpoint (#1836).
 * It sits above the API-key / custom-OAuth-app tabs because connecting is
 * what most visitors came for, and most of them need nothing from either tab:
 * Claude.ai, Claude Desktop, ChatGPT, Cursor and Claude Code paste the bare
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
 * so the Claude Code OAuth one-liner keeps its core-profile form here.
 *
 * Key-bearing snippets (.mcp.json, ChatGPT Bearer, Codex CLI) stay on the
 * API-key tab next to the key they embed (MCPConfigBlock).
 */

"use client";

import { useId, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";
import { CopyIconButton } from "@/components/common/CopyIconButton";
import { Section } from "@/components/common/Section";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { useCopyFeedback } from "@/hooks/useCopyFeedback";
import { useToast } from "@/hooks/use-toast";
import {
  buildClaudeOAuthCommand,
  mcpEndpoints,
  withFullProfile,
} from "@/lib/mcp/url";

const PYTHON_SDK_URL = "https://github.com/kagura-ai/kagura-memory-python-sdk";
const MCP_CLIENTS_DOCS_URL =
  "https://github.com/kagura-ai/memory-cloud/blob/main/docs/mcp-clients.md";

export function McpConnectionCard() {
  const t = useTranslations("mcpConnection");
  const tApiKeys = useTranslations("apiKeys");
  const tCommon = useTranslations("common");
  const { currentWorkspaceId, workspaces } = useWorkspace();
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
  const [allTools, setAllTools] = useState(false);
  const allToolsSwitchId = useId();
  const allToolsHelpId = useId();
  const shownUrl = allTools ? withFullProfile(mcpUrl) : mcpUrl;
  const claudeCodeCommand = buildClaudeOAuthCommand(shownUrl);

  // Pinning only means something when there is another workspace the OAuth
  // connector could otherwise drift to.
  const pinnedUrlToOffer = workspaces.length > 1 ? pinnedUrl : null;
  const shownPinnedUrl =
    pinnedUrlToOffer && allTools
      ? withFullProfile(pinnedUrlToOffer)
      : pinnedUrlToOffer;

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

  return (
    <Section title={`🔗 ${t("title")}`} description={t("description")}>
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
              onCheckedChange={setAllTools}
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

        {/* Pin an OAuth connector to this workspace (multi-workspace users) */}
        {shownPinnedUrl && (
          <div className="space-y-2 border-t border-gray-200 dark:border-gray-700 pt-3">
            <p className="text-xs text-gray-600 dark:text-gray-400">
              {t("followsWorkspace")}
            </p>
            <Collapsible>
              <CollapsibleTrigger asChild>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="gap-1 text-xs"
                >
                  <ChevronDown className="w-3 h-3" />
                  {t("pinToggle")}
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent className="mt-2 space-y-2">
                <div className="flex items-center gap-2">
                  <code className="flex-1 bg-gray-50 dark:bg-gray-800 px-3 py-2 rounded border border-gray-200 dark:border-gray-700 text-xs font-mono break-all">
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
                <p className="text-xs text-gray-600 dark:text-gray-400">
                  {t("pinHint")}
                </p>
              </CollapsibleContent>
            </Collapsible>
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
