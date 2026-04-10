"use client";

import { useState } from "react";
import { useTranslations } from "next-intl";
import { createOAuth2Client } from "@/lib/api/oauth";
import { useToast } from "@/hooks/use-toast";
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
import { X } from "lucide-react";

interface CreateCustomOAuthAppDialogProps {
  isOpen: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess: () => void;
}

export function CreateCustomOAuthAppDialog({
  isOpen,
  onOpenChange,
  onSuccess,
}: CreateCustomOAuthAppDialogProps) {
  const t = useTranslations("customApps");
  const tCommon = useTranslations("common");
  const { toast } = useToast();

  const [appName, setAppName] = useState("");
  const [redirectUris, setRedirectUris] = useState<string[]>([""]);
  const [error, setError] = useState<string | null>(null);

  const resetForm = () => {
    setAppName("");
    setRedirectUris([""]);
    setError(null);
  };

  const handleClose = () => {
    resetForm();
    onOpenChange(false);
  };

  const handleCreate = async () => {
    try {
      setError(null);

      if (!appName.trim()) {
        setError(t("appNameRequired"));
        return;
      }

      const validUris = redirectUris.filter((uri) => uri.trim());
      if (validUris.length === 0) {
        setError(t("redirectUriRequired"));
        return;
      }

      await createOAuth2Client({
        provider: "custom",
        client_name: appName,
        redirect_uris: validUris,
      });

      resetForm();
      onSuccess();

      toast({
        title: tCommon("success"),
        description: t("createCustomSuccess"),
      });
    } catch (err: unknown) {
      setError(
        err instanceof Error ? err.message : "Failed to create OAuth app",
      );
    }
  };

  return (
    <AlertDialog open={isOpen} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-w-2xl">
        <AlertDialogHeader>
          <AlertDialogTitle>{t("createCustomOAuthTitle")}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium mb-1">
                  {t("appNameLabel")}
                </label>
                <input
                  type="text"
                  value={appName}
                  onChange={(e) => setAppName(e.target.value)}
                  placeholder={t("appNamePlaceholder")}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100"
                />
              </div>

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
                      }}
                      placeholder={t("redirectUriPlaceholder")}
                      className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 text-sm"
                    />
                    {redirectUris.length > 1 && (
                      <button
                        onClick={() => {
                          const newUris = redirectUris.filter(
                            (_, i) => i !== index,
                          );
                          setRedirectUris(newUris);
                        }}
                        className="p-2 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded"
                        title={t("removeUri")}
                      >
                        <X className="w-4 h-4" />
                      </button>
                    )}
                  </div>
                ))}
                <button
                  onClick={() => setRedirectUris([...redirectUris, ""])}
                  className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                >
                  + {t("addRedirectUri")}
                </button>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-2">
                  💡 {t("redirectUriHint")}
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
          <AlertDialogCancel onClick={handleClose}>
            {tCommon("cancel")}
          </AlertDialogCancel>
          <AlertDialogAction onClick={handleCreate}>
            {t("createApp")}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
