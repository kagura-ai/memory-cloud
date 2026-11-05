"use client";

import { useTranslations } from "next-intl";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { GuideCodeBlock } from "@/components/common/GuideCodeBlock";
import { mcpEndpoints } from "@/lib/mcp/url";

export function SdkSetupGuide({ kind, mcpUrl: configuredUrl }: { kind: "api" | "secrets"; mcpUrl?: string }) {
  const t = useTranslations("sdkSetup");
  const { mcpUrl: defaultUrl } = mcpEndpoints(process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080", null);
  const mcpUrl = configuredUrl ?? defaultUrl;
  const secrets = kind === "secrets";
  const typescript = secrets
    ? `import { SecretClient } from "kagura-memory";

const client = SecretClient.fromMcpUrl({
  apiKey: process.env.KAGURA_API_KEY,
  mcpUrl: ${JSON.stringify(mcpUrl)},
});
try {
  console.log(await client.listSecrets());
} finally {
  await client.close();
}`
    : `import { KaguraClient } from "kagura-memory";

const client = new KaguraClient({
  apiKey: process.env.KAGURA_API_KEY,
  mcpUrl: ${JSON.stringify(mcpUrl)},
});
try {
  console.log(await client.listContexts());
} finally {
  await client.close();
}`;
  const python = `import asyncio
import os
${secrets ? "from kagura_memory.secrets.client import SecretClient" : "from kagura_memory import KaguraClient"}

async def main():
    async with ${secrets ? "SecretClient.from_mcp_url" : "KaguraClient"}(
        api_key=os.environ["KAGURA_API_KEY"],
        mcp_url=${JSON.stringify(mcpUrl)},
    ) as client:
        print(await client.${secrets ? "list_secrets" : "list_contexts"}())

asyncio.run(main())`;


  const typescriptKeySetup = `import { KeyManager, SecretClient, type KeyStore } from "kagura-memory";

export async function registerDeviceKey(store: KeyStore) {
  const keys = new KeyManager({ store, profile: "default" });
  if (!(await keys.hasKey())) await keys.enroll();

  const client = SecretClient.fromMcpUrl({
    apiKey: process.env.KAGURA_API_KEY,
    mcpUrl: ${JSON.stringify(mcpUrl)},
  });
  try {
    const publicKey = await client.registerPubkey(await keys.getRecipient(), "laptop");
    return { publicKey, fingerprint: await keys.fingerprint() };
  } finally {
    await client.close();
  }
}`;
  const pythonKeySetup = `import asyncio
import os
from kagura_memory.secrets.client import SecretClient
from kagura_memory.secrets.keymanager import KeyManager

async def main():
    keys = KeyManager(profile="default")
    if not keys.has_key():
        keys.enroll()
    async with SecretClient.from_mcp_url(
        api_key=os.environ["KAGURA_API_KEY"],
        mcp_url=${JSON.stringify(mcpUrl)},
    ) as client:
        public_key = await client.register_pubkey(keys.get_recipient(), label="laptop")
        print(public_key.id, keys.fingerprint())

asyncio.run(main())`;

  return (
    <section className="mb-6 space-y-3">
      <h3 className="text-base font-semibold">{t("title")}</h3>
      <p className="text-sm text-muted-foreground">{t(secrets ? "secretsIntro" : "apiIntro")}</p>
      <Tabs defaultValue="typescript">
        <TabsList aria-label={t("language")}>
          <TabsTrigger value="typescript">TypeScript / JavaScript</TabsTrigger>
          <TabsTrigger value="python">Python</TabsTrigger>
        </TabsList>
        <TabsContent value="typescript" className="space-y-3 pt-3">
          <GuideCodeBlock text={secrets ? "npm install kagura-memory age-encryption" : "npm install kagura-memory"} label={t("copyInstall")} />
          <p className="text-sm text-muted-foreground">{t("credentials")}</p>
          {secrets && (
            <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-4 dark:border-slate-600 dark:bg-slate-800">
              <h4 className="font-semibold">{t("keySetup.title")}</h4>
              <p className="text-sm text-muted-foreground">{t("keySetup.typescript")}</p>
              <GuideCodeBlock text={typescriptKeySetup} label={t("keySetup.copy")} />
              <p className="text-sm text-muted-foreground">{t("keySetup.approval")}</p>
              <p className="text-xs text-muted-foreground">{t("keySetup.custody")}</p>
            </div>
          )}
          <h4 className="text-sm font-medium">{t(secrets ? "secretExample" : "apiExample")}</h4>
          <GuideCodeBlock text={typescript} label={t("copyExample")} />
          <a href="https://github.com/kagura-ai/kagura-memory-typescript-sdk" target="_blank" rel="noopener noreferrer" className="inline-flex min-h-10 items-center text-sm text-primary underline">TypeScript SDK</a>
        </TabsContent>
        <TabsContent value="python" className="space-y-3 pt-3">
          <GuideCodeBlock text={secrets ? 'pip install "kagura-memory[secret]"' : "pip install kagura-memory"} label={t("copyInstall")} />
          <p className="text-sm text-muted-foreground">{t("credentials")}</p>
          {secrets && (
            <div className="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-4 dark:border-slate-600 dark:bg-slate-800">
              <h4 className="font-semibold">{t("keySetup.title")}</h4>
              <p className="text-sm text-muted-foreground">{t("keySetup.python")}</p>
              <GuideCodeBlock text={pythonKeySetup} label={t("keySetup.copy")} />
              <p className="text-sm text-muted-foreground">{t("keySetup.approval")}</p>
              <p className="text-xs text-muted-foreground">{t("keySetup.custody")}</p>
            </div>
          )}
          <h4 className="text-sm font-medium">{t(secrets ? "secretExample" : "apiExample")}</h4>
          <GuideCodeBlock text={python} label={t("copyExample")} />
          <a href="https://github.com/kagura-ai/kagura-memory-python-sdk" target="_blank" rel="noopener noreferrer" className="inline-flex min-h-10 items-center text-sm text-primary underline">Python SDK</a>
        </TabsContent>
      </Tabs>
    </section>
  );
}
