"use client";

import { useTranslations } from "next-intl";
import { BookOpen, ChevronDown } from "lucide-react";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Button } from "@/components/ui/button";
import { GuideCodeBlock } from "@/components/common/GuideCodeBlock";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { MCP_RESOURCE_TOOLS } from "@/lib/mcp/toolPresets";
import { mcpEndpoints, withFullProfile, withAdditionalTools } from "@/lib/mcp/url";

export function ResourceSetupGuide() {
  const t = useTranslations("resourceSetup");
  const { currentWorkspace } = useWorkspace();
  const endpoints = mcpEndpoints(process.env.NEXT_PUBLIC_API_URL || "http://localhost:8080", currentWorkspace?.id ?? null);
  const templateUrl = withAdditionalTools(endpoints.pinnedUrl ?? endpoints.mcpUrl, MCP_RESOURCE_TOOLS.join(","));
  const fullUrl = withFullProfile(endpoints.pinnedUrl ?? endpoints.mcpUrl);
  return (
    <Collapsible className="mb-6 rounded-lg border">
      <CollapsibleTrigger asChild>
        <Button variant="ghost" className="group min-h-11 w-full justify-start gap-2 px-4">
          <BookOpen aria-hidden="true" className="h-4 w-4" />
          {t("title")}
          <ChevronDown aria-hidden="true" className="ml-auto h-4 w-4 transition-transform group-data-[state=open]:rotate-180" />
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="space-y-4 p-4 pt-3">
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">{t("connectTitle")}</h3>
          <p className="text-sm text-muted-foreground">{t("connectHelp")}</p>
          <Tabs defaultValue="ingestion">
            <TabsList aria-label={t("modeLabel")}>
              <TabsTrigger value="ingestion">{t("ingestionMode")}</TabsTrigger>
              <TabsTrigger value="full">{t("fullMode")}</TabsTrigger>
            </TabsList>
            <TabsContent value="ingestion" className="space-y-2">
              <p className="text-xs text-muted-foreground">{t("ingestionHelp")}</p>
              <GuideCodeBlock text={templateUrl} label={t("copyUrl")} />
            </TabsContent>
            <TabsContent value="full" className="space-y-2">
              <p className="text-xs text-muted-foreground">{t("fullHelp")}</p>
              <GuideCodeBlock text={fullUrl} label={t("copyUrl")} />
            </TabsContent>
          </Tabs>
          <p className="text-xs text-muted-foreground">{t("refreshHelp")}</p>
        </div>
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">{t("inspectTitle")}</h3>
          <GuideCodeBlock text={t("inspectPrompt")} label={t("copyPrompt")} />
        </div>
        <div className="space-y-2">
          <h3 className="text-sm font-semibold">{t("createTitle")}</h3>
          <GuideCodeBlock text={t("createPrompt")} label={t("copyPrompt")} />
          <p className="text-xs text-muted-foreground">{t("availability")}</p>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
