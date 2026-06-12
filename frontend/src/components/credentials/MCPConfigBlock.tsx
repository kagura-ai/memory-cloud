"use client";

/**
 * MCPConfigBlock
 *
 * Renders a copy-pasteable MCP config snippet for the API Keys tab. The
 * Authorization header carries the live API key during the visibility
 * window, masked by default. Three tabs cover the distinct config shapes:
 * Claude Code (standard `mcpServers` JSON — also what Cursor and other
 * mcpServers-based clients consume, so they no longer get a separate tab),
 * ChatGPT (custom-connector instructions), and Codex CLI (install one-liner
 * + manual TOML). The user's last choice is persisted to localStorage.
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
import { Copy, Check, Eye, EyeOff, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useToast } from "@/hooks/use-toast";
import { useRevealableSecret } from "@/hooks/useRevealableSecret";
import type { MemberAPIKey } from "@/lib/api/member-credentials";

// Cursor consumes the identical standard `mcpServers` JSON as Claude Code, so
// it no longer gets its own tab — the single "Claude Code" tab covers both.
// A legacy localStorage value of "cursor" is no longer a member here; it falls
// back to DEFAULT_CLIENT ("claude-code") via readStoredClient, which renders the
// same JSON — so the merge is transparent to returning users.
const MCP_CLIENTS = ["claude-code", "chatgpt", "codex"] as const;
type MCPClient = (typeof MCP_CLIENTS)[number];

const STORAGE_KEY = "kagura_last_mcp_client";
const DEFAULT_CLIENT: MCPClient = "claude-code";

// Fallback prefix when no key has been created yet (so the visible mask
// still LOOKS like a Kagura key, not a generic OpenAI-style "sk-" key).
const FALLBACK_KEY_PREFIX = "kag_";
const MASK_BODY = "•••••••••••";
const PLACEHOLDER_KEY = "YOUR_API_KEY";

// Codex CLI plugin install one-liner. The handle `kagura-memory@kagura-memory-cloud`
// is derived from `.agents/plugins/marketplace.json` as `<plugin.name>@<marketplace.name>`
// — i.e. plugins[0].name (`kagura-memory`) joined to the top-level marketplace
// `name` (`kagura-memory-cloud`). If either name in that manifest changes, this
// string must move in lockstep (the marketplace guard test in backend/tests/ pins
// the shape, but the install command itself is rendered text and not covered).
export const CODEX_INSTALL_COMMAND =
  "codex plugin install kagura-memory@kagura-memory-cloud";

/**
 * Strip the workspace-scoped `/w/<workspaceId>` suffix from an MCP URL,
 * yielding the bare `…/mcp` endpoint the Claude Code OAuth one-liner targets
 * (OAuth resolves the workspace at login — issue #988). Idempotent: a URL that
 * is already bare passes through unchanged.
 *
 * This is the single source of the derivation. Prefer passing the bare URL
 * directly via the `mcpBaseUrl` prop (the caller in APIKeysTabPanel already
 * computes `baseUrl + "/mcp"`); this helper is the fallback when only the
 * workspace-scoped URL is available, and keeps the regex in one tested place
 * instead of inlined at each future call site.
 */
export function toBareMcpUrl(mcpUrl: string): string {
  return mcpUrl.replace(/\/w\/[^/]+$/, "");
}

export interface MCPConfigBlockProps {
  /**
   * The user's API key. `null` when no key has been created yet OR the
   * visibility window has expired (the placeholder JSON is rendered and
   * Copy is disabled in both cases).
   */
  apiKey: MemberAPIKey | null;
  /** The MCP endpoint URL the JSON snippet should embed. */
  mcpUrl: string;
  /**
   * The bare `…/mcp` endpoint (no `/w/<workspaceId>` suffix) for the OAuth
   * one-liner. Optional: when omitted it is derived from `mcpUrl` via
   * {@link toBareMcpUrl}. The caller passes its already-computed base URL so
   * the production path needs no regex.
   */
  mcpBaseUrl?: string;
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
 * The Claude Code / Cursor (JSON) branch uses `JSON.stringify(...)` which
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

/**
 * Build the Codex CLI `~/.codex/config.toml` snippet for the manual config
 * block. The snippet declares an HTTP-transport MCP server using the same
 * URL + Bearer pair as the JSON variants.
 *
 * TOML escape contract: values are emitted as TOML basic strings (`"..."`).
 * TOML basic strings forbid raw `\` and `"`, and also forbid raw control
 * characters (U+0000–U+001F except tab) and ``. We escape in a fixed
 * order:
 *   1. `\` → `\\` (must come FIRST so escapes added below aren't doubled)
 *   2. `"` → `\"`
 *   3. common whitespace controls: `\n` → `\\n`, `\r` → `\\r`, `\t` → `\\t`
 *   4. remaining control chars (U+0000–U+001F, U+007F) → `\uXXXX`
 *
 * The dev-mode warn below is a tripwire that surfaces in development when a
 * caller passes risky input — it complements (does not replace) the runtime
 * escape, which is now the actual safety net. Real API keys are base64-ish
 * (none of these characters), so the escape is defense in depth against a
 * future refactor that lets user-controllable content reach this helper.
 */
export function buildTomlConfig(mcpUrl: string, authValue: string): string {
  if (process.env.NODE_ENV === "development") {
    const unsafeRe = /[ -]/;
    if (unsafeRe.test(mcpUrl) || unsafeRe.test(authValue)) {
      // eslint-disable-next-line no-console
      console.warn(
        "[MCPConfigBlock.buildTomlConfig] mcpUrl or authValue contains " +
          "control characters. They will be escaped (TOML basic strings " +
          "forbid raw control chars), but these inputs are expected to be " +
          "server-issued — investigate why a refactor introduced them.",
      );
    }
  }
  const escape = (s: string) =>
    s
      .replace(/\\/g, "\\\\")
      .replace(/"/g, '\\"')
      .replace(/\n/g, "\\n")
      .replace(/\r/g, "\\r")
      .replace(/\t/g, "\\t")
      .replace(/[ -]/g, (ch) => {
        const hex = ch.charCodeAt(0).toString(16).padStart(4, "0");
        return `\\u${hex}`;
      });
  return [
    "[mcp_servers.kagura-memory]",
    'type = "http"',
    `url = "${escape(mcpUrl)}"`,
    `bearer_token = "${escape(authValue)}"`,
  ].join("\n");
}

export function MCPConfigBlock({
  apiKey,
  mcpUrl,
  mcpBaseUrl,
}: MCPConfigBlockProps) {
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

  // Codex tab — TOML snippet variants. Mirror the JSON pair: displayToml
  // uses visibleAuthValue (mask / placeholder / live), copyToml always
  // uses the live key. Same disabled-when-null contract.
  const displayToml = useMemo(
    () => buildTomlConfig(mcpUrl, visibleAuthValue),
    [mcpUrl, visibleAuthValue],
  );
  const copyToml = useMemo(() => {
    if (liveKey === null) return null;
    return buildTomlConfig(mcpUrl, liveKey);
  }, [mcpUrl, liveKey]);

  // Claude Code OAuth one-liner (#988). Targets the BARE /mcp endpoint —
  // OAuth resolves the workspace at login, so the workspace-scoped
  // `/w/<workspaceId>` suffix is stripped (if mcpUrl is already bare, the
  // regex is a no-op). The command is env-aware via mcpUrl (localhost in dev,
  // the production host in prod) and needs no API key, so it renders and
  // copies even when the key window is closed.
  const claudeOAuthCommand = useMemo(
    () =>
      `claude mcp add --transport http kagura-memory ${mcpBaseUrl ?? toBareMcpUrl(mcpUrl)}`,
    [mcpBaseUrl, mcpUrl],
  );

  // Track which Copy button the user pressed last, so the Check icon only
  // flashes on the button they actually clicked. Without this, the shared
  // `copied` flag from useRevealableSecret causes the Codex tab's install
  // and manual-TOML buttons to BOTH flash Check when either is pressed
  // (Copilot review flagged this on PR #817). The hook still owns the 2s
  // timer; this just disambiguates the visual target.
  const [lastCopied, setLastCopied] = useState<
    "json" | "toml" | "install" | "oauth" | null
  >(null);

  // Single copy path for all four buttons. They differ only in the value
  // copied, the per-target "copied" affordance (`target`), and the success
  // title; the guard (skip when the value is null/disabled), the routing
  // through useRevealableSecret.copy → copyText (#987 execCommand fallback,
  // and cancellation of any prior 60s auto-clear so a pending timer can't wipe
  // the freshly-copied value), and the destructive failure toast are identical.
  // On hard failure copyText has already exhausted its fallback, so we surface
  // an actionable i18n hint (the snippet stays visible in the <pre> for manual
  // copy) rather than leaking the raw DOM exception string.
  const runCopy = async (
    value: string | null,
    target: "json" | "toml" | "install" | "oauth",
    successTitle: string,
  ) => {
    if (value === null) return;
    try {
      await copy(value);
      setLastCopied(target);
      toast({ title: successTitle, description: t("mcpConfigCopiedHint") });
    } catch {
      toast({
        title: tCommon("error"),
        description: tCommon("copyFailedManualHint"),
        variant: "destructive",
      });
    }
  };

  const handleCopy = () => runCopy(copyJson, "json", t("mcpConfigCopied"));
  const handleTomlCopy = () => runCopy(copyToml, "toml", t("mcpConfigCopied"));
  const handleInstallCopy = () =>
    runCopy(CODEX_INSTALL_COMMAND, "install", t("codexInstallCopied"));
  const handleOAuthCopy = () =>
    runCopy(claudeOAuthCommand, "oauth", t("claudeOAuthCopied"));

  return (
    <div className="space-y-3" suppressHydrationWarning>
      <Tabs value={client} onValueChange={(v) => setClient(v as MCPClient)}>
        <TabsList>
          <TabsTrigger value="claude-code">
            {t("clients.claudeCode")}
          </TabsTrigger>
          <TabsTrigger value="chatgpt">{t("clients.chatgpt")}</TabsTrigger>
          <TabsTrigger value="codex">{t("clients.codex")}</TabsTrigger>
        </TabsList>

        {/* JSON-shape tabs share one render path. Codex needs an independent
            path because its UI is a static install command + a collapsible
            manual TOML — buildJsonConfig doesn't apply. */}
        {MCP_CLIENTS.filter((c) => c !== "codex").map((c) => (
          <TabsContent key={c} value={c} className="mt-3 space-y-3">
            {/* Issue #988: Claude Code OAuth one-liner (recommended) — no API
                key needed; Claude Code runs the OAuth browser flow on first
                use. Only for the claude-code client; ChatGPT does not use the
                `claude mcp add` CLI. The existing .mcp.json JSON stays below as
                the manual / API-key alternative. */}
            {c === "claude-code" && (
              <div>
                <h4 className="text-sm font-medium mb-2">
                  {t("claudeOAuthHeading")}
                </h4>
                <div className="relative">
                  <pre className="bg-gray-900 text-gray-100 p-3 pr-12 rounded overflow-x-auto text-xs whitespace-pre-wrap break-all">
                    {claudeOAuthCommand}
                  </pre>
                  <div className="absolute top-2 right-2">
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={handleOAuthCopy}
                      title={t("copyClaudeOAuthCommand")}
                      aria-label={t("copyClaudeOAuthCommand")}
                      className="text-gray-300 hover:text-white hover:bg-gray-700/50"
                    >
                      {copied && lastCopied === "oauth" ? (
                        <Check className="w-4 h-4 text-green-400" />
                      ) : (
                        <Copy className="w-4 h-4" />
                      )}
                    </Button>
                  </div>
                </div>
                <p className="text-xs text-blue-700 dark:text-blue-300 mt-2">
                  💡 {t("claudeOAuthHint")}
                </p>
              </div>
            )}
            {c === "claude-code" && (
              <p className="text-xs font-medium text-gray-600 dark:text-gray-400 pt-1">
                {t("claudeManualConfigLabel")}
              </p>
            )}
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
                  {copied && lastCopied === "json" ? (
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

        {/* Codex CLI tab: install one-liner (recommended) + collapsible
            manual TOML fallback. The install path handles plugin sign-in;
            the manual TOML covers fallback when the plugin install does
            not auto-configure the MCP server endpoint. */}
        <TabsContent value="codex" className="mt-3 space-y-3">
          <div>
            <h4 className="text-sm font-medium mb-2">
              {t("codexInstallHeading")}
            </h4>
            <div className="relative">
              <pre className="bg-gray-900 text-gray-100 p-3 pr-12 rounded overflow-x-auto text-xs whitespace-pre-wrap break-all">
                {CODEX_INSTALL_COMMAND}
              </pre>
              <div className="absolute top-2 right-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={handleInstallCopy}
                  title={t("copyInstallCommand")}
                  aria-label={t("copyInstallCommand")}
                  className="text-gray-300 hover:text-white hover:bg-gray-700/50"
                >
                  {copied && lastCopied === "install" ? (
                    <Check className="w-4 h-4 text-green-400" />
                  ) : (
                    <Copy className="w-4 h-4" />
                  )}
                </Button>
              </div>
            </div>
            <p className="text-xs text-blue-700 dark:text-blue-300 mt-2">
              💡 {t("codexInstallHint")}
            </p>
          </div>

          <Collapsible>
            <CollapsibleTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="gap-1 text-xs"
              >
                <ChevronDown className="w-3 h-3" />
                {t("codexManualConfigToggle")}
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="mt-2">
              <div className="relative">
                <pre className="bg-gray-900 text-gray-100 p-3 pr-20 rounded overflow-x-auto text-xs whitespace-pre-wrap break-all">
                  {displayToml}
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
                    onClick={handleTomlCopy}
                    disabled={disabled}
                    title={
                      disabled
                        ? t("mcpConfigHiddenCopyDisabled")
                        : t("copyManualConfig")
                    }
                    aria-label={
                      disabled
                        ? t("mcpConfigHiddenCopyDisabled")
                        : t("copyManualConfig")
                    }
                    className="text-gray-300 hover:text-white hover:bg-gray-700/50"
                  >
                    {copied && lastCopied === "toml" ? (
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
            </CollapsibleContent>
          </Collapsible>
        </TabsContent>
      </Tabs>
    </div>
  );
}
