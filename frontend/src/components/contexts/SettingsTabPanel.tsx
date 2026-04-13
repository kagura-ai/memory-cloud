/**
 * SettingsTabPanel
 *
 * Self-contained panel for the Settings tab in the consolidated context detail page.
 * Contains basic info, AI config, privacy with sticky save bar.
 * Extracted from contexts/[id]/settings/page.tsx (#232).
 */

"use client";

import { useEffect, useState, useCallback } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Save,
  AlertCircle,
  Loader2,
  Brain,
  Lock,
  Copy,
  Info,
} from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { getContext, updateContext } from "@/lib/api/contexts";
import type { Context } from "@/lib/types/context";
import { useAuth } from "@/contexts/AuthContext";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { CONTEXT_TEMPLATES, getTemplate } from "@/lib/templates/usage-guide";

interface SettingsTabPanelProps {
  contextId: string;
  context: Context;
  onContextUpdated: (context: Context) => void;
}

export function SettingsTabPanel({
  contextId,
  context,
  onContextUpdated,
}: SettingsTabPanelProps) {
  const { user } = useAuth();
  const { currentWorkspace } = useWorkspace();
  const { toast } = useToast();
  const t = useTranslations("contextSettings");
  const tCommon = useTranslations("common");

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Form state — initialised from context prop to avoid isDirty flash
  const [displayName, setDisplayName] = useState(context.display_name || "");
  const [description, setDescription] = useState(context.description || "");
  const [summary, setSummary] = useState(context.summary || "");
  const [usageGuide, setUsageGuide] = useState(context.usage_guide || "");
  const [isPrivate, setIsPrivate] = useState(context.is_private ?? true);
  const [isPublic, setIsPublic] = useState(context.is_public ?? false);
  const [resourceId, setResourceId] = useState("");

  const [idCopied, setIdCopied] = useState(false);
  const [privacyDialogOpen, setPrivacyDialogOpen] = useState(false);
  const [pendingPrivacyChange, setPendingPrivacyChange] = useState<
    boolean | null
  >(null);

  const applyContextData = useCallback((data: Context) => {
    setDisplayName(data.display_name || "");
    setDescription(data.description || "");
    setSummary(data.summary || "");
    setUsageGuide(data.usage_guide || "");
    setIsPrivate(data.is_private ?? true);
    setIsPublic(data.is_public ?? false);
    setResourceId("");
  }, []);

  // Sync form state when context prop changes
  useEffect(() => {
    applyContextData(context);
  }, [context, applyContextData]);

  const refreshContext = useCallback(async () => {
    try {
      const data = await getContext(contextId);
      applyContextData(data);
      onContextUpdated(data);
    } catch {
      // Silent refresh
    }
  }, [contextId, applyContextData, onContextUpdated]);

  const handleSave = async () => {
    if (isPublic && !context.is_public && !resourceId.trim()) {
      toast({
        title: t("resourceIdRequired"),
        description: t("resourceIdRequiredDesc"),
        variant: "destructive",
      });
      return;
    }

    try {
      setSaving(true);
      setError(null);

      let resource_id: string | undefined = undefined;
      if (isPublic && !context.is_public) {
        resource_id = resourceId.trim();
      }

      await updateContext(context.id, {
        display_name: displayName.trim(),
        description: description.trim(),
        summary: summary.trim(),
        usage_guide: usageGuide.trim(),
        is_private: isPrivate,
        is_public: isPublic,
        resource_id: resource_id,
      });

      toast({
        title: t("savedTitle"),
        description: t("savedDesc"),
      });
      await refreshContext();
    } catch (err: unknown) {
      toast({
        title: t("saveFailedTitle"),
        description: err instanceof Error ? err.message : t("saveFailedDesc"),
        variant: "destructive",
        duration: 6000,
      });
    } finally {
      setSaving(false);
    }
  };

  const handlePrivacyToggle = (newIsPrivate: boolean) => {
    if (!context?.is_private && newIsPrivate) {
      setPendingPrivacyChange(newIsPrivate);
      setPrivacyDialogOpen(true);
    } else {
      setIsPrivate(newIsPrivate);
      markDirty();
    }
  };

  const confirmPrivacyChange = () => {
    if (pendingPrivacyChange !== null) {
      setIsPrivate(pendingPrivacyChange);
      markDirty();
    }
    setPrivacyDialogOpen(false);
    setPendingPrivacyChange(null);
  };

  const isOwner = currentWorkspace?.current_user_role === "owner";

  const [isDirty, setIsDirty] = useState(false);
  const markDirty = useCallback(() => setIsDirty(true), []);

  // Reset dirty flag when context prop changes (after save/discard)
  useEffect(() => {
    setIsDirty(false);
  }, [context]);

  return (
    <>
      <div className="space-y-6">
        {error && (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        {/* Basic Information */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Info className="h-5 w-5" />
              {t("basicInfoTitle")}
            </CardTitle>
            <CardDescription>{t("basicInfoDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label>{t("contextId")}</Label>
              <div className="flex items-center gap-2">
                <Input
                  value={context.id}
                  disabled
                  className="font-mono text-xs"
                />
                <Button
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(context.id);
                      setIdCopied(true);
                      setTimeout(() => setIdCopied(false), 1500);
                    } catch {
                      toast({
                        title: t("copyFailed"),
                        variant: "destructive",
                      });
                    }
                  }}
                >
                  {idCopied ? (
                    <span className="text-xs text-green-600 dark:text-green-400">
                      {t("copied")}
                    </span>
                  ) : (
                    <Copy className="h-4 w-4" />
                  )}
                </Button>
              </div>
              <p className="text-sm text-muted-foreground">
                {t("contextIdHelp")}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("contextName")}</Label>
              <Input value={context.name} disabled className="font-mono" />
              <p className="text-sm text-muted-foreground">
                {t("contextNameHelp")}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("displayName")}</Label>
              <Input
                value={displayName}
                onChange={(e) => {
                  setDisplayName(e.target.value);
                  markDirty();
                }}
                placeholder={context.name}
                maxLength={200}
              />
              <p className="text-sm text-muted-foreground">
                {t("displayNameHelp")}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("descriptionLabel")}</Label>
              <Textarea
                placeholder={t("descriptionPlaceholder")}
                value={description}
                onChange={(e) => {
                  setDescription(e.target.value);
                  markDirty();
                }}
                rows={2}
              />
            </div>
          </CardContent>
        </Card>

        {/* AI Configuration */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Brain className="h-5 w-5" />
              {t("aiConfigTitle")}
            </CardTitle>
            <CardDescription>{t("aiConfigDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label>{t("templateLabel")}</Label>
              <Select
                onValueChange={(templateId) => {
                  const template = getTemplate(templateId);
                  if (template) {
                    setSummary(template.summary);
                    setUsageGuide(template.usage_guide);
                    markDirty();
                  }
                }}
              >
                <SelectTrigger>
                  <SelectValue placeholder={t("templatePlaceholder")} />
                </SelectTrigger>
                <SelectContent>
                  {CONTEXT_TEMPLATES.map((tpl) => (
                    <SelectItem key={tpl.id} value={tpl.id}>
                      <div className="flex items-center gap-2">
                        <span className="text-xs px-2 py-0.5 rounded bg-muted">
                          {tpl.category}
                        </span>
                        <span className="font-medium">{tpl.name}</span>
                      </div>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-sm text-muted-foreground">
                {t("templateHelp")}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("summaryLabel")}</Label>
              <Textarea
                placeholder={t("summaryPlaceholder")}
                value={summary}
                onChange={(e) => {
                  setSummary(e.target.value);
                  markDirty();
                }}
                maxLength={500}
                rows={3}
              />
              <p className="text-sm text-muted-foreground">
                {t("summaryHelp", { count: summary.length })}
              </p>
            </div>

            <div className="space-y-2">
              <Label>{t("usageGuideLabel")}</Label>
              <Textarea
                placeholder={t("usageGuidePlaceholder")}
                value={usageGuide}
                onChange={(e) => {
                  setUsageGuide(e.target.value);
                  markDirty();
                }}
                maxLength={2000}
                rows={6}
              />
              <p className="text-sm text-muted-foreground">
                {t("usageGuideHelp", { count: usageGuide.length })}
              </p>
            </div>
          </CardContent>
        </Card>

        {/* Privacy & Access Control */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Lock className="h-5 w-5" />
              {t("privacyTitle")}
            </CardTitle>
            <CardDescription>{t("privacyDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center justify-between p-4 border rounded-lg">
              <div className="flex-1">
                <p className="font-medium text-sm">
                  {isPrivate
                    ? `🔒 ${t("privateContext")}`
                    : `👥 ${t("sharedContext")}`}
                </p>
                <p className="text-sm text-muted-foreground">
                  {isPrivate ? t("privateContextDesc") : t("sharedContextDesc")}
                </p>
              </div>
              <Button
                variant={isPrivate ? "outline" : "default"}
                size="sm"
                onClick={() => handlePrivacyToggle(!isPrivate)}
              >
                {isPrivate ? t("makeShared") : t("makePrivate")}
              </Button>
            </div>

            {isOwner &&
              (isPrivate ? (
                <Alert>
                  <AlertCircle className="h-4 w-4" />
                  <AlertDescription>{t("makeSharedFirst")}</AlertDescription>
                </Alert>
              ) : isPublic || context.is_public ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between p-4 border rounded-lg">
                    <div className="flex-1">
                      <p className="font-medium text-sm">
                        🌍 {t("publicContext")}
                      </p>
                      <p className="text-sm text-muted-foreground">
                        {t("publicContextDesc")}
                      </p>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="border-red-300 text-red-700 hover:bg-red-50 dark:border-red-700 dark:text-red-400 dark:hover:bg-red-950"
                      onClick={() => {
                        setIsPublic(false);
                        markDirty();
                      }}
                    >
                      {t("unpublish")}
                    </Button>
                  </div>

                  {!isPublic && context.is_public && (
                    <Alert>
                      <AlertCircle className="h-4 w-4" />
                      <AlertDescription>
                        {t("unpublishPending")}
                      </AlertDescription>
                    </Alert>
                  )}

                  {isPublic && context.resource_id && (
                    <Alert>
                      <AlertCircle className="h-4 w-4" />
                      <AlertTitle>{t("publicSearchApi")}</AlertTitle>
                      <AlertDescription className="space-y-2">
                        <div>
                          <p className="text-xs font-medium mb-1">
                            {t("resourceIdLabel")}:
                          </p>
                          <code className="block p-2 bg-muted rounded text-xs font-mono break-all">
                            {context.resource_id}
                          </code>
                        </div>
                        <div>
                          <p className="text-xs font-medium mb-1">
                            {t("resourceIdEndpoint")}
                          </p>
                          <code className="block p-2 bg-muted rounded text-xs font-mono break-all">
                            POST /api/v1/public/{contextId}/search
                          </code>
                        </div>
                        <div className="flex gap-3 mt-2 text-xs">
                          <a
                            href="/workspace/integrations/credentials?tab=resource-tokens"
                            className="text-primary underline hover:text-primary/80"
                          >
                            {t("manageResourceTokens")}
                          </a>
                          <a
                            href="https://github.com/kagura-ai/kagura-memory-python-sdk"
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-primary underline hover:text-primary/80"
                          >
                            {t("pythonSdk")}
                          </a>
                        </div>
                      </AlertDescription>
                    </Alert>
                  )}
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="space-y-2">
                    <Label>
                      {t("resourceIdLabel")}{" "}
                      <span className="text-red-500">*</span>
                    </Label>
                    <Input
                      placeholder={t("resourceIdPlaceholder")}
                      value={resourceId}
                      onChange={(e) => {
                        const value = e.target.value
                          .toLowerCase()
                          .replace(/[^a-z0-9_]/g, "");
                        setResourceId(value);
                      }}
                      className="font-mono text-sm"
                    />
                    <p className="text-sm text-muted-foreground">
                      {t("resourceIdHelp")}
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    className="w-full"
                    disabled={!resourceId.trim()}
                    onClick={() => {
                      if (resourceId.trim()) {
                        setIsPublic(true);
                        markDirty();
                      }
                    }}
                  >
                    🌍 {t("makePublic")}
                  </Button>
                </div>
              ))}
          </CardContent>
        </Card>
      </div>

      {/* Privacy Change Confirmation Dialog */}
      <AlertDialog open={privacyDialogOpen} onOpenChange={setPrivacyDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("makePrivateTitle")}</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2 text-sm text-muted-foreground">
                <div>{t("makePrivateDesc")}</div>
                <div className="text-yellow-600 dark:text-yellow-400 font-medium">
                  {t("makePrivateWarning")}
                </div>
                <div>{t("makePrivateReaddNote")}</div>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel
              onClick={() => {
                setPrivacyDialogOpen(false);
                setPendingPrivacyChange(null);
              }}
            >
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={confirmPrivacyChange}
              className="bg-yellow-600 hover:bg-yellow-700"
            >
              {t("makePrivate")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Sticky Save Bar — only rendered when dirty */}
      {isDirty && (
        <div className="fixed bottom-0 left-0 right-0 z-50 border-t bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
          <div className="container flex items-center justify-between py-3 px-4 max-w-4xl mx-auto">
            <p className="text-sm text-muted-foreground">
              {t("unsavedChanges")}
            </p>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={refreshContext}>
                {t("discard")}
              </Button>
              <Button size="sm" onClick={handleSave} disabled={saving}>
                {saving ? (
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                ) : (
                  <Save className="h-4 w-4 mr-2" />
                )}
                {saving ? t("saving") : t("saveChanges")}
              </Button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
