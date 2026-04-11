"use client";

/**
 * MCPConfigBlock
 *
 * Renders a copy-pasteable MCP config snippet for the API Keys tab. The
 * Authorization header carries the live API key during the visibility
 * window, masked by default. Three client variants (Claude Code, Cursor,
 * ChatGPT custom connector) are switchable via entity tabs; the user's
 * last choice is persisted to localStorage.
 *
 * Security model (from CSO + CDO + DX-Lead pre-review consensus):
 * - The plaintext key is the SAME secret already rendered in the single-key
 *   display block during the visibility window. This component does not add
 *   a new exposure vector; it adds a second visual location and a JSON-shaped
 *   clipboard payload.
 * - Visual default is masked (`Bearer sk-•••••••••••`). Show toggle reveals
 *   the live value. Copy always writes the live value regardless of visual
 *   state.
 * - 60s clipboard auto-clear (defense in depth) via useRevealableSecret.
 * - When apiKey is null (visibility window expired) the JSON shows the
 *   `Bearer YOUR_API_KEY` placeholder and the Copy button is disabled with
 *   a tooltip pointing the user at regenerate.
 * - On a visible→hidden transition the useEffect inside this component
 *   force-hides any revealed state — the live key MUST NOT remain in the
 *   DOM after the window closes (regression net flagged Critical by CSO).
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { Copy, Check, Eye, EyeOff } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/hooks/use-toast";
import { useRevealableSecret } from "@/hooks/useRevealableSecret";
import type { MemberAPIKey } from "@/lib/api/member-credentials";

const MCP_CLIENTS = ["claude-code", "cursor", "chatgpt"] as const;
type MCPClient = (typeof MCP_CLIENTS)[number];

const STORAGE_KEY = "kagura_last_mcp_client";
const DEFAULT_CLIENT: MCPClient = "claude-code";

const MASKED_KEY = "sk-•••••••••••";
const PLACEHOLDER_KEY = "YOUR_API_KEY";

export interface MCPConfigBlockProps {
  /**
   * The user's API key. `null` when no key has been created yet OR the
   * visibility window has expired (the placeholder JSON is rendered and
   * Copy is disabled in both cases).
   */
  apiKey: MemberAPIKey | null;
  /** The MCP endpoint URL the JSON snippet should embed. */
  mcpUrl: string;
}

function readStoredClient(): MCPClient {
  if (typeof window === "undefined") return DEFAULT_CLIENT;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored && (MCP_CLIENTS as readonly string[]).includes(stored)) {
      return stored as MCPClient;
    }
  } catch {
    // Some browsers (Safari private mode) throw on localStorage access.
  }
  return DEFAULT_CLIENT;
}

/**
 * Build the displayed/copied snippet for one of the 3 client variants.
 *
 * SECURITY CONTRACT: `mcpUrl` and `authValue` MUST be server-issued or
 * known constants. They are NEVER user-controlled. Specifically:
 * - `mcpUrl` is `${baseUrl}/mcp/w/${currentWorkspaceId}` where baseUrl is
 *   from NEXT_PUBLIC_API_URL (build-time env) and currentWorkspaceId is a
 *   backend-issued UUID.
 * - `authValue` is either the live API key (backend-issued), the masked
 *   constant `MASKED_KEY`, or the placeholder constant `PLACEHOLDER_KEY`.
 *
 * The Claude Code / Cursor branches use `JSON.stringify(...)` which
 * automatically escapes the values. The ChatGPT branch uses raw template
 * interpolation inside a comment block — if either input ever contains
 * newlines, `*\/`, or other unsafe characters, the rendered comment will
 * break. The dev-mode guard below catches refactor regressions early so
 * a future change that introduces user-controllable content into either
 * input fails loudly in development before reaching production.
 */
function buildJsonConfig(
  client: MCPClient,
  mcpUrl: string,
  authValue: string,
): string {
  if (process.env.NODE_ENV === "development") {
    const unsafeRe = /[\n\r]|\*\//;
    if (unsafeRe.test(mcpUrl) || unsafeRe.test(authValue)) {
      // eslint-disable-next-line no-console
      console.warn(
        "[MCPConfigBlock.buildJsonConfig] mcpUrl or authValue contains " +
          "newlines or comment-terminator sequences. These inputs MUST be " +
          "server-issued. If a refactor introduced user-controllable content, " +
          "either escape it before passing here or change the ChatGPT branch " +
          "to use a structured format instead of raw template interpolation.",
      );
    }
  }

  // The serverName is intentionally consistent across clients — it's the
  // identifier the user references when calling MCP tools, and matching it
  // to the project name (kagura-memory) keeps doc cross-references stable.
  const serverName = "kagura-memory";

  if (client === "chatgpt") {
    // ChatGPT custom connectors take a flatter shape — typically just the
    // endpoint URL and authorization header at the top level. We render a
    // comment block explaining how to paste it into the connector UI. See
    // the SECURITY CONTRACT in the JSDoc above for input trust expectations.
    return [
      "// ChatGPT → Settings → Custom Connectors → New connector",
      "// Paste the URL and Authorization header into the form fields:",
      `//   URL: ${mcpUrl}`,
      `//   Authorization: Bearer ${authValue}`,
    ].join("\n");
  }

  return JSON.stringify(
    {
      mcpServers: {
        [serverName]: {
          type: "http",
          url: mcpUrl,
          headers: {
            Authorization: `Bearer ${authValue}`,
          },
        },
      },
    },
    null,
    2,
  );
}

export function MCPConfigBlock({ apiKey, mcpUrl }: MCPConfigBlockProps) {
  const t = useTranslations("apiKeys");
  const { toast } = useToast();
  const { revealed, toggle, copy, copied, hide } = useRevealableSecret();

  // Track which client tab is active. Read localStorage on mount only —
  // SSR-safe because the initial state computation runs in useState init,
  // which executes on the client during hydration.
  const [client, setClient] = useState<MCPClient>(DEFAULT_CLIENT);
  useEffect(() => {
    setClient(readStoredClient());
  }, []);

  // Persist client choice on every change (after mount).
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(STORAGE_KEY, client);
    } catch {
      // Same Safari private mode caveat as readStoredClient.
    }
  }, [client]);

  const liveKey =
    apiKey?.is_visible && apiKey.plaintext_key ? apiKey.plaintext_key : null;
  const disabled = liveKey === null;

  // CRITICAL: when the live key transitions to null (visibility window
  // expired or backend hide), force any revealed state back to false so the
  // plaintext NEVER remains in the DOM across that transition.
  useEffect(() => {
    if (liveKey === null && revealed) {
      hide();
    }
  }, [liveKey, revealed, hide]);

  // The value embedded in the visible JSON. Three states:
  //   - hidden window  → PLACEHOLDER_KEY ("YOUR_API_KEY")
  //   - masked, visible → MASKED_KEY ("sk-•••••••••••")
  //   - revealed, visible → liveKey
  const visibleAuthValue = useMemo(() => {
    if (liveKey === null) return PLACEHOLDER_KEY;
    if (revealed) return liveKey;
    return MASKED_KEY;
  }, [liveKey, revealed]);

  // The displayed JSON snippet (what the user sees on screen).
  const displayJson = useMemo(
    () => buildJsonConfig(client, mcpUrl, visibleAuthValue),
    [client, mcpUrl, visibleAuthValue],
  );

  // The JSON snippet copied to clipboard — always uses the live key when
  // available, regardless of revealed state. When disabled, this is unused
  // because handleCopy short-circuits.
  const copyJson = useMemo(() => {
    if (liveKey === null) return null;
    return buildJsonConfig(client, mcpUrl, liveKey);
  }, [client, mcpUrl, liveKey]);

  const handleCopy = async () => {
    if (copyJson === null) return;
    try {
      await copy(copyJson);
      toast({
        title: t("mcpConfigCopied"),
        description: t("mcpConfigCopiedHint"),
      });
    } catch (err) {
      toast({
        title: t("mcpConfigCopied"),
        description: (err as Error).message,
        variant: "destructive",
      });
    }
  };

  return (
    <div className="space-y-3">
      <Tabs value={client} onValueChange={(v) => setClient(v as MCPClient)}>
        <TabsList>
          <TabsTrigger value="claude-code">
            {t("clients.claudeCode")}
          </TabsTrigger>
          <TabsTrigger value="cursor">{t("clients.cursor")}</TabsTrigger>
          <TabsTrigger value="chatgpt">{t("clients.chatgpt")}</TabsTrigger>
        </TabsList>

        {MCP_CLIENTS.map((c) => (
          <TabsContent key={c} value={c} className="mt-3">
            <div className="relative">
              <pre className="bg-gray-900 text-gray-100 p-3 pr-20 rounded overflow-x-auto text-xs whitespace-pre-wrap break-all">
                {displayJson}
              </pre>
              <div className="absolute top-2 right-2 flex gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={toggle}
                  disabled={disabled}
                  title={revealed ? t("hideKey") : t("showKey")}
                  aria-label={revealed ? t("hideKey") : t("showKey")}
                  aria-pressed={revealed}
                  className="text-gray-300 hover:text-white hover:bg-gray-700/50"
                >
                  {revealed ? (
                    <EyeOff className="w-4 h-4" />
                  ) : (
                    <Eye className="w-4 h-4" />
                  )}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={handleCopy}
                  disabled={disabled}
                  title={
                    disabled
                      ? t("mcpConfigHiddenCopyDisabled")
                      : t("copyConfig")
                  }
                  aria-label={
                    disabled
                      ? t("mcpConfigHiddenCopyDisabled")
                      : t("copyConfig")
                  }
                  className="text-gray-300 hover:text-white hover:bg-gray-700/50"
                >
                  {copied ? (
                    <Check className="w-4 h-4 text-green-400" />
                  ) : (
                    <Copy className="w-4 h-4" />
                  )}
                </Button>
              </div>
            </div>
            <p className="text-xs text-blue-700 dark:text-blue-300 mt-2">
              💡 {t("mcpConfigHint")}
            </p>
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
