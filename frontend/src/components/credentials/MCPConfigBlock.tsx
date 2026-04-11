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

// Fallback prefix when no key has been created yet (so the visible mask
// still LOOKS like a Kagura key, not a generic OpenAI-style "sk-" key).
const FALLBACK_KEY_PREFIX = "kag_";
const MASK_BODY = "•••••••••••";
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
  const tCommon = useTranslations("common");
  const { toast } = useToast();
  const { revealed, toggle, copy, copied, hide } = useRevealableSecret();

  // Track which client tab is active.
  //
  // SSR + localStorage handling: the lazy `useState` initializer runs on
  // BOTH the server (where window is undefined → DEFAULT_CLIENT) and the
  // client (where window exists → readStoredClient()). When the user has
  // a non-default client stored, server and client produce different
  // initial values, which is a deliberate hydration mismatch — React
  // patches the DOM to the client value in a single commit, with no
  // visible "claude-code → cursor" flash. The wrapping div opts out of
  // the hydration warning that would otherwise fire (the mismatch is
  // intentional and contained to this component).
  //
  // Alternatives considered:
  //  - useState(DEFAULT_CLIENT) + useEffect(setStored): correct hydration
  //    but causes a visible flash on every mount AND a redundant
  //    setItem(DEFAULT_CLIENT) write before the stored value is applied.
  //  - useSyncExternalStore: heavier, doesn't actually solve the
  //    server/client mismatch any more cleanly than the lazy initializer.
  //  - Render placeholder until hydrated: degrades the perceived load
  //    time of the MCP setup guide.
  const [client, setClient] = useState<MCPClient>(() => readStoredClient());

  // Persist client choice on every change. Fires once on mount with the
  // resolved initial value (idempotent if it equals the stored value, a
  // first-write if no value was stored). Subsequent fires happen when
  // the user clicks a different tab.
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(STORAGE_KEY, client);
    } catch {
      // Some browsers (Safari private mode) throw on localStorage access.
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

  // Derive the masked display from the actual key_prefix when available
  // (e.g. "kag_•••••••••••"), so the visible mask matches the real key
  // shape and doesn't mislead the user with a generic "sk-" prefix.
  // Falls back to "kag_" when apiKey is null because no key has been
  // created yet — the placeholder branch is always YOUR_API_KEY anyway,
  // so the maskedKey value is only used during the visible window.
  const maskedKey = useMemo(
    () => `${apiKey?.key_prefix ?? FALLBACK_KEY_PREFIX}${MASK_BODY}`,
    [apiKey?.key_prefix],
  );

  // The value embedded in the visible JSON. Three states:
  //   - hidden window  → PLACEHOLDER_KEY ("YOUR_API_KEY")
  //   - masked, visible → maskedKey ("kag_•••••••••••")
  //   - revealed, visible → liveKey
  const visibleAuthValue = useMemo(() => {
    if (liveKey === null) return PLACEHOLDER_KEY;
    if (revealed) return liveKey;
    return maskedKey;
  }, [liveKey, revealed, maskedKey]);

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
      // Use the common error title (NOT the success title) and narrow err
      // safely so non-Error rejections (DOMException, strings) don't
      // produce empty descriptions.
      toast({
        title: tCommon("error"),
        description: err instanceof Error ? err.message : String(err),
        variant: "destructive",
      });
    }
  };

  return (
    <div className="space-y-3" suppressHydrationWarning>
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
