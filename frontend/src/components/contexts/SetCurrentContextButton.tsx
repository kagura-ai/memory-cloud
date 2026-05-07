"use client";

import { Check } from "lucide-react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";

interface SetCurrentContextButtonProps {
  contextId: string;
}

export function SetCurrentContextButton({
  contextId,
}: SetCurrentContextButtonProps) {
  const t = useTranslations("contexts");
  const router = useRouter();
  const searchParams = useSearchParams();
  const pathname = usePathname();

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    const params = new URLSearchParams(searchParams?.toString() ?? "");
    params.set("context", contextId);
    router.replace(`${pathname}?${params.toString()}`);
  };

  return (
    <Button
      variant="ghost"
      size="sm"
      className="h-7 px-2 text-xs"
      onClick={handleClick}
      aria-label={t("setAsCurrent")}
      title={t("setAsCurrent")}
    >
      <Check className="h-3 w-3 mr-1" aria-hidden="true" />
      {t("setAsCurrent")}
    </Button>
  );
}
