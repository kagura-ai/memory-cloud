"use client";

import { useTranslations } from "next-intl";
import { BookOpen, ChevronDown } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { Button } from "@/components/ui/button";
import { SdkSetupGuide } from "@/components/credentials/SdkSetupGuide";

export function SecretSetupGuide() {
  const t = useTranslations("sdkSetup");
  return (
    <Collapsible className="mb-6 rounded-lg border">
      <CollapsibleTrigger asChild>
        <Button variant="ghost" className="group min-h-11 w-full justify-start gap-2 px-4">
          <BookOpen aria-hidden="true" className="h-4 w-4" />
          {t("startSecrets")}
          <ChevronDown aria-hidden="true" className="ml-auto h-4 w-4 transition-transform group-data-[state=open]:rotate-180" />
        </Button>
      </CollapsibleTrigger>
      <CollapsibleContent className="px-4 pt-3">
        <SdkSetupGuide kind="secrets" />
      </CollapsibleContent>
    </Collapsible>
  );
}
