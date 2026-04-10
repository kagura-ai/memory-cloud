"use client";

import { useState, useEffect, useCallback } from "react";
import { useTranslations } from "next-intl";
import { updateOAuth2Client, OAuth2Client } from "@/lib/api/oauth";
import { useToast } from "@/hooks/use-toast";
import { X, Pencil } from "lucide-react";
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

interface EditOAuthClientDialogProps {
  client: OAuth2Client | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
}

export function EditOAuthClientDialog({
  client,
  open,
  onOpenChange,
  onSuccess,
}: EditOAuthClientDialogProps) {
  const t = useTranslations("customApps");
  const tCommon = useTranslations("common");
  const { toast } = useToast();

  const [clientName, setClientName] = useState("");
  const [redirectUris, setRedirectUris] = useState<string[]>([""]);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);

  // Pre-fill when client changes or dialog opens
  useEffect(() => {
    if (client && open) {
      setClientName(client.client_name);
      setRedirectUris(
        client.redirect_uris.length > 0 ? [...client.redirect_uris] : [""],
      );
      setError(null);
      setFieldErrors({});
    }
  }, [client, open]);

  const handleSave = useCallback(async () => {
    if (!client) return;

    setError(null);
    setFieldErrors({});

    // Client-side validation
    if (!clientName.trim()) {
      setFieldErrors({ clientName: t("appNameRequired") });
      return;
    }

    const validUris = redirectUris.filter((uri) => uri.trim());
    if (validUris.length === 0) {
      setFieldErrors({ redirectUris: t("redirectUriRequired") });
      return;
    }

    try {
      setSaving(true);
      await updateOAuth2Client(client.client_id, {
        client_name: clientName.trim(),
        redirect_uris: validUris.map((u) => u.trim()),
      });

      toast({
        title: tCommon("success"),
        description: t("editSuccess"),
      });
      onOpenChange(false);
      onSuccess();
    } catch (err: unknown) {
      const apiError = err as {
        message?: string;
        details?: { detail?: string | Array<{ loc?: string[]; msg?: string }> };
      };
      // Handle Pydantic 422 field-level errors
      const detail = apiError?.details?.detail;
      if (Array.isArray(detail)) {
        const newFieldErrors: Record<string, string> = {};
        for (const item of detail) {
          const field = item.loc?.slice(-1)[0];
          if (field) {
            newFieldErrors[field] = item.msg || "Invalid value";
          }
        }
        if (Object.keys(newFieldErrors).length > 0) {
          setFieldErrors(newFieldErrors);
          setError(t("editFieldError"));
          return;
        }
      }
      setError(
        apiError?.message ||
          (apiError?.details?.detail as string) ||
          "Failed to update OAuth client",
      );
    } finally {
      setSaving(false);
    }
  }, [
    client,
    clientName,
    redirectUris,
    t,
    tCommon,
    toast,
    onOpenChange,
    onSuccess,
  ]);

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-w-2xl">
        <AlertDialogHeader>
          <AlertDialogTitle className="flex items-center gap-2">
            <Pencil className="w-4 h-4" />
            {t("editOAuthTitle")}
          </AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-4">
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {t("editOAuthDesc")}
              </p>

              {/* Client Name */}
              <div>
                <label className="block text-sm font-medium mb-1">
                  {t("clientNameLabel")}
                </label>
                <input
                  type="text"
                  value={clientName}
                  onChange={(e) => {
                    setClientName(e.target.value);
                    setFieldErrors((prev) => {
                      const next = { ...prev };
                      delete next.clientName;
                      delete next.client_name;
                      return next;
                    });
                  }}
                  placeholder={t("appNamePlaceholder")}
                  className={`w-full px-3 py-2 border rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 ${
                    fieldErrors.clientName || fieldErrors.client_name
                      ? "border-red-500"
                      : "border-gray-300 dark:border-gray-600"
                  }`}
                />
                {(fieldErrors.clientName || fieldErrors.client_name) && (
                  <p className="text-xs text-red-600 dark:text-red-400 mt-1">
                    {fieldErrors.clientName || fieldErrors.client_name}
                  </p>
                )}
              </div>

              {/* Redirect URIs */}
              <div>
                <label className="block text-sm font-medium mb-1">
                  {t("redirectUris")}
                </label>
                {redirectUris.map((uri, index) => (
                  <div key={index} className="flex items-center gap-2 mb-2">
                    <input
                      type="text"
                      value={uri}
                      onChange={(e) => {
                        const newUris = [...redirectUris];
                        newUris[index] = e.target.value;
                        setRedirectUris(newUris);
                        setFieldErrors((prev) => {
                          const next = { ...prev };
                          delete next.redirectUris;
                          delete next.redirect_uris;
                          return next;
                        });
                      }}
                      placeholder={t("redirectUriPlaceholder")}
                      className={`flex-1 px-3 py-2 border rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 text-sm ${
                        fieldErrors.redirectUris || fieldErrors.redirect_uris
                          ? "border-red-500"
                          : "border-gray-300 dark:border-gray-600"
                      }`}
                    />
                    {redirectUris.length > 1 && (
                      <button
                        onClick={() => {
                          setRedirectUris(
                            redirectUris.filter((_, i) => i !== index),
                          );
                        }}
                        className="p-2 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded"
                        title={t("removeUri")}
                      >
                        <X className="w-4 h-4" />
                      </button>
                    )}
                  </div>
                ))}
                {(fieldErrors.redirectUris || fieldErrors.redirect_uris) && (
                  <p className="text-xs text-red-600 dark:text-red-400 mb-1">
                    {fieldErrors.redirectUris || fieldErrors.redirect_uris}
                  </p>
                )}
                <button
                  onClick={() => setRedirectUris([...redirectUris, ""])}
                  className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                >
                  + {t("addRedirectUri")}
                </button>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                  {t("redirectUriHint")}
                </p>
              </div>

              {error && (
                <p className="text-sm text-red-600 dark:text-red-400">
                  {error}
                </p>
              )}
            </div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={saving}>
            {tCommon("cancel")}
          </AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => {
              e.preventDefault();
              handleSave();
            }}
            disabled={saving}
          >
            {saving ? t("saving") : t("saveChanges")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
