/**
 * `apiKeys` Codex CLI messages through the real ICU parser (#1624).
 *
 * MCPConfigBlock.test.tsx mocks `useTranslations` to echo the key, so neither
 * the wording nor en/ja parity is checked there. The Codex tab's snippets name
 * an environment variable (`KAGURA_API_KEY`) the user has to export before the
 * command works, so both locales must carry it literally — and neither may
 * describe the plugin as signing in or configuring the server (it ships a
 * skill only).
 */
import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import { CODEX_BEARER_TOKEN_ENV_VAR } from "@/components/credentials/MCPConfigBlock";

import en from "./en.json";
import ja from "./ja.json";

describe("apiKeys Codex CLI messages", () => {
  it.each([
    ["en", en],
    ["ja", ja],
  ] as const)(
    "%s names the env var and the config file",
    (locale, messages) => {
      const t = createTranslator({
        locale,
        messages,
        namespace: "apiKeys",
        onError: (error) => {
          throw error;
        },
      });

      expect(t("codexAddHeading")).toBeTruthy();
      expect(t("codexAddHint")).toContain(CODEX_BEARER_TOKEN_ENV_VAR);
      expect(t("codexAddHint")).toContain("config.toml");
      expect(t("codexManualConfigToggle")).toContain("~/.codex/config.toml");
      expect(t("codexManualConfigHint")).toBeTruthy();
      expect(t("copyCodexAddCommand")).toBeTruthy();
      expect(t("codexAddCopied")).toBeTruthy();
    },
  );

  it("no longer offers the plugin install as the Codex setup path", () => {
    for (const messages of [en, ja]) {
      const apiKeys = messages.apiKeys as Record<string, unknown>;
      expect(apiKeys).not.toHaveProperty("codexInstallHeading");
      expect(apiKeys).not.toHaveProperty("codexInstallHint");
      expect(apiKeys).not.toHaveProperty("codexInstallCopied");
      expect(apiKeys).not.toHaveProperty("copyInstallCommand");
    }
  });
});
