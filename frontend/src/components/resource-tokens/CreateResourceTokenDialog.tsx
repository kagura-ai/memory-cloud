/**
 * Create Resource Token Dialog
 *
 * Issue #242 - Create resource token with quota selection
 * Shows plaintext token ONLY once (one-time display)
 */

import { useState, useEffect } from "react";
import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { AlertCircle, Check, CheckCircle, Copy, Info } from "lucide-react";
import {
  createResourceToken,
  type ResourceToken,
  type ResourceTokenCreateResponse,
} from "@/lib/api/resource-tokens";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { useWorkspace } from "@/contexts/WorkspaceContext";
import { getContexts, type Context } from "@/lib/api/contexts";

import {
  MAX_QUOTA_PER_TOKEN,
  getMaxTokens,
  getMaxQuotaCapacity,
} from "@/config/resource-tokens";

interface CreateResourceTokenDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess: () => void;
  currentTokens: ResourceToken[];
}

// Plan-based quota defaults and limits
const QUOTA_CONFIG = {
  free: { default: 0, min: 0, max: 0 },
  basic: { default: 500, min: 1, max: 1000 },
  pro: { default: 1000, min: 1, max: 10000 },
};

export function CreateResourceTokenDialog({
  isOpen,
  onClose,
  onSuccess,
  currentTokens,
}: CreateResourceTokenDialogProps) {
  const t = useTranslations("resourceTokens");
  const { currentWorkspace } = useWorkspace();
  const planName = (currentWorkspace?.plan_name ||
    "free") as keyof typeof QUOTA_CONFIG;
  const quotaConfig = QUOTA_CONFIG[planName] || QUOTA_CONFIG.basic;

  // Calculate remaining quota (use centralized constants)
  const maxPerToken = MAX_QUOTA_PER_TOKEN;
  const maxTokens = getMaxTokens(planName);
  const maxTotalQuota = getMaxQuotaCapacity(planName);
  const usedQuota = currentTokens
    .filter((t) => t.status === "active")
    .reduce((sum, t) => sum + t.quota_events_per_hour, 0);
  const remainingQuota = Math.max(0, maxTotalQuota - usedQuota);
  const quotaMax = Math.min(remainingQuota, maxPerToken); // Max for this token: min(remaining, 10000)
  const quotaDefault = Math.min(remainingQuota, maxPerToken); // Default: same as max

  const [resourceId, setResourceId] = useState("");
  const [description, setDescription] = useState("");
  const [quotaInput, setQuotaInput] = useState(quotaDefault.toString()); // Fixed: use quotaMax, not remainingQuota
  const [loading, setLoading] = useState(false);
  const [loadingContexts, setLoadingContexts] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Contexts with resource_id for selection
  const [resourceContexts, setResourceContexts] = useState<Context[]>([]);

  // One-time display state
  const [createdToken, setCreatedToken] =
    useState<ResourceTokenCreateResponse | null>(null);
  const [copied, setCopied] = useState(false);

  // Load contexts with resource_id when dialog opens
  useEffect(() => {
    if (isOpen) {
      loadResourceContexts();
    }
  }, [isOpen]);

  const loadResourceContexts = async () => {
    try {
      setLoadingContexts(true);
      const data = await getContexts();
      const ctxs = data.contexts.filter((ctx) => ctx.resource_id);
      setResourceContexts(ctxs);
    } catch (err) {
      if (process.env.NODE_ENV === "development") {
        console.error("Failed to load contexts:", err);
      }
      // TODO: Send to logging service in production
    } finally {
      setLoadingContexts(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    if (!resourceId.trim()) {
      setError(t("createDialog.resourceIdRequired"));
      return;
    }

    const quotaNum = parseInt(quotaInput, 10);
    if (isNaN(quotaNum) || quotaNum < 1 || quotaNum > quotaMax) {
      setError(
        `Quota must be between 1 and ${quotaMax.toLocaleString()} (${remainingQuota.toLocaleString()} remaining)`,
      );
      return;
    }

    try {
      setLoading(true);
      setError(null);

      const response = await createResourceToken({
        resource_id: resourceId.trim(),
        description: description.trim() || null,
        quota_events_per_hour: quotaNum,
      });

      // Show one-time display
      setCreatedToken(response);
    } catch (err) {
      if (process.env.NODE_ENV === "development") {
        console.error("Failed to create resource token:", err);
      }
      setError(
        err instanceof Error ? err.message : "Failed to create resource token",
      );
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async () => {
    if (createdToken) {
      await navigator.clipboard.writeText(createdToken.token);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const handleClose = () => {
    setResourceId("");
    setDescription("");
    setQuotaInput(quotaDefault.toString()); // Reset to default (not remainingQuota)
    setError(null);
    setCreatedToken(null);
    setCopied(false);
    onClose();
  };

  const handleDone = () => {
    handleClose();
    onSuccess();
  };

  // One-time display mode
  if (createdToken) {
    return (
      <Dialog open={isOpen} onOpenChange={handleClose}>
        <DialogContent className="sm:max-w-[600px]">
          <DialogHeader>
            <DialogTitle>{t("successDialog.title")}</DialogTitle>
            <DialogDescription>
              {t("successDialog.description")}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4 py-4">
            <Alert className="border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20">
              <CheckCircle className="h-4 w-4 text-green-500" />
              <AlertTitle className="text-green-800 dark:text-green-200">
                {t("successDialog.alertTitle")}
              </AlertTitle>
              <AlertDescription className="text-green-700">
                {t("successDialog.alertDescription")}
              </AlertDescription>
            </Alert>

            <div className="space-y-4">
              {/* Resource ID */}
              <div className="space-y-2">
                <Label>{t("successDialog.resourceId")}</Label>
                <code className="text-xs bg-slate-100 dark:bg-slate-800 px-3 py-2 rounded block">
                  {createdToken.resource_id}
                </code>
              </div>

              {/* Token */}
              <div className="space-y-2">
                <Label>{t("successDialog.token")}</Label>
                <div className="flex gap-2">
                  <Input
                    value={createdToken.token}
                    readOnly
                    className="font-mono text-sm bg-white dark:bg-gray-800 dark:text-gray-100"
                  />
                  <Button
                    onClick={handleCopy}
                    variant="outline"
                    className={
                      copied
                        ? "bg-green-50 dark:bg-green-900/30 border-green-300 dark:border-green-700"
                        : ""
                    }
                  >
                    {copied ? (
                      <>
                        <Check className="h-4 w-4 text-green-600 mr-1" />
                        <span className="text-green-600 text-sm">
                          {t("successDialog.copied")}
                        </span>
                      </>
                    ) : (
                      <>
                        <Copy className="h-4 w-4 mr-1" />
                        <span className="text-sm">
                          {t("successDialog.copy")}
                        </span>
                      </>
                    )}
                  </Button>
                </div>
              </div>

              {/* Quota */}
              <div className="space-y-2">
                <Label>{t("successDialog.quota")}</Label>
                <Input
                  value={createdToken.quota_events_per_hour.toLocaleString()}
                  readOnly
                  className="bg-white dark:bg-gray-800 dark:text-gray-100"
                />
              </div>

              <p className="text-xs text-slate-500">
                {t("successDialog.helpText")}{" "}
                <code className="bg-slate-100 dark:bg-slate-800 px-1 py-0.5 rounded">
                  {t("successDialog.headerName")}
                </code>{" "}
                {t("successDialog.header")}
              </p>
            </div>
          </div>

          <DialogFooter>
            <div className="w-full space-y-2">
              <Button
                onClick={handleDone}
                className="w-full"
                disabled={!copied}
              >
                {copied
                  ? t("successDialog.done")
                  : t("successDialog.copyFirst")}
              </Button>
              {!copied && (
                <Button
                  variant="ghost"
                  onClick={handleClose}
                  className="w-full text-xs text-slate-500"
                >
                  {t("successDialog.cancelWarning")}
                </Button>
              )}
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }

  // Create form mode
  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>{t("createDialog.title")}</DialogTitle>
        </DialogHeader>

        <form onSubmit={handleSubmit}>
          <div className="space-y-4 py-4">
            {error && (
              <Alert variant="destructive">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            <div className="space-y-2">
              <Label htmlFor="resource_id">
                {t("createDialog.resourceId")} *
              </Label>
              {resourceContexts.length > 0 ? (
                <Select
                  value={resourceId}
                  onValueChange={setResourceId}
                  disabled={loading || loadingContexts}
                >
                  <SelectTrigger id="resource_id">
                    <SelectValue
                      placeholder={t("createDialog.resourceIdPlaceholder")}
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {resourceContexts.map((ctx) => (
                      <SelectItem value={ctx.resource_id ?? ""} key={ctx.id}>
                        {ctx.display_name || ctx.name}
                        {ctx.resource_id && (
                          <span className="text-slate-500 text-xs ml-2">
                            ({ctx.resource_id})
                          </span>
                        )}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : loadingContexts ? (
                <div className="text-sm text-slate-500 py-2">
                  {t("loading")}
                </div>
              ) : (
                <div className="rounded-lg border border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-900/20 p-3">
                  <p className="text-sm text-amber-800 dark:text-amber-200">
                    {t("noPublicContexts")}
                  </p>
                  <p className="text-xs text-amber-700 mt-1">
                    {t("noPublicContextsDesc")}
                  </p>
                </div>
              )}
              <div className="bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-800 rounded p-3 mt-2">
                <div className="flex gap-2">
                  <Info className="h-4 w-4 text-blue-600 flex-shrink-0 mt-0.5" />
                  <div className="text-xs text-blue-800 dark:text-blue-200 space-y-1">
                    <p>
                      <strong>{t("createDialog.n1DesignNote")}</strong>
                    </p>
                    <p>{t("createDialog.n1DesignDetail")}</p>
                    <p className="text-blue-600 dark:text-blue-300 italic">
                      {t("createDialog.n1DesignExample")}
                    </p>
                  </div>
                </div>
              </div>
            </div>

            <div className="space-y-2">
              <Label htmlFor="description">
                {t("createDialog.description")}
              </Label>
              <Textarea
                id="description"
                placeholder={t("createDialog.descriptionPlaceholder")}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                disabled={loading}
                rows={2}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="quota">{t("createDialog.quota")}</Label>
              <Input
                id="quota"
                type="number"
                min={1}
                max={quotaMax}
                value={quotaInput}
                onChange={(e) => setQuotaInput(e.target.value)}
                disabled={loading}
                required
              />
              <p className="text-xs text-slate-500">
                {remainingQuota > 0 ? (
                  t("createDialog.quotaRemaining", {
                    remaining: remainingQuota.toLocaleString(),
                    unit: t("eventsPerHour"),
                    maxPerToken: maxPerToken.toLocaleString(),
                  })
                ) : (
                  <span className="text-red-600">
                    {t("createDialog.quotaLimitReached")}
                  </span>
                )}
              </p>
              <p className="text-xs text-slate-400 italic">
                {t("createDialog.quotaNote")}
              </p>
            </div>
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={handleClose}
              disabled={loading}
            >
              {t("createDialog.cancel")}
            </Button>
            <Button
              type="submit"
              disabled={loading || resourceContexts.length === 0}
            >
              {loading ? t("createDialog.creating") : t("createDialog.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
