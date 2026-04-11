/**
 * OAuthAppCard Component
 *
 * Presentational component for a single OAuth app card.
 * Renders Client ID, Client Secret (with visibility/hide logic), and Action buttons.
 */

"use client";

import { useTranslations } from "next-intl";
import {
  Copy,
  Check,
  EyeOff,
  Pencil,
  RefreshCw,
  Trash2,
  AlertTriangle,
} from "lucide-react";
import { OAuth2Client } from "@/lib/api/oauth";
import { ActionButton } from "@/components/common/ActionButton";
import { formatDateTime, formatRelativeTime } from "@/lib/utils/datetime";

interface OAuthAppCardProps {
  app: OAuth2Client;
  copyKey: string; // prefix for copy-feedback keys, e.g. "claude", "chatgpt", "custom-{client_id}"
  onCopy: (text: string, key: string) => void;
  /** Returns true while the given key is in the "just copied" feedback window. */
  isCopied: (key: string) => boolean;
  onHide: (clientId: string) => void;
  onRegenerate: (clientId: string, provider: string) => void;
  onDelete: (clientId: string) => void;
  onEdit: (app: OAuth2Client) => void;
  timezone?: string;
  locale: string;
}

export function OAuthAppCard({
  app,
  copyKey,
  onCopy,
  isCopied,
  onHide,
  onRegenerate,
  onDelete,
  onEdit,
  timezone,
  locale,
}: OAuthAppCardProps) {
  const t = useTranslations("customApps");
  const tCommon = useTranslations("common");

  return (
    <div className="space-y-3">
      {/* Client ID */}
      <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700">
        <div className="flex items-center justify-between mb-1">
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {t("clientId")}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <code className="flex-1 bg-white dark:bg-gray-900 px-2 py-1 rounded text-xs font-mono break-all">
            {app.client_id}
          </code>
          <button
            onClick={() => onCopy(app.client_id, `${copyKey}-id`)}
            className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
            title={t("copyClientId")}
          >
            {isCopied(`${copyKey}-id`) ? (
              <Check className="w-4 h-4 text-green-600" />
            ) : (
              <Copy className="w-4 h-4" />
            )}
          </button>
        </div>
        <div className="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700">
          <p className="text-xs text-gray-500 dark:text-gray-400 mb-1">
            {t("redirectUri")}:
          </p>
          {app.redirect_uris.map((uri, idx) => (
            <p
              key={idx}
              className="text-xs text-gray-600 dark:text-gray-400 font-mono break-all"
            >
              {uri}
            </p>
          ))}
        </div>
      </div>

      {/* Client Secret */}
      {app.is_visible && app.plaintext_secret ? (
        <div className="bg-green-50 dark:bg-green-900/20 p-3 rounded border border-green-200 dark:border-green-800">
          <div className="flex items-center justify-between mb-1">
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
              {t("clientSecret")}
            </span>
            {app.visibility_expires_at &&
              (() => {
                const expiresAt = new Date(app.visibility_expires_at);
                const daysUntil =
                  (expiresAt.getTime() - Date.now()) / (1000 * 60 * 60 * 24);
                if (daysUntil <= 0) return null;
                return (
                  <span className="text-xs text-yellow-600 dark:text-yellow-400 flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3" />
                    {daysUntil <= 30
                      ? t("hideInTime", {
                          time: formatRelativeTime(
                            app.visibility_expires_at,
                            timezone,
                            locale,
                            false,
                          ),
                        })
                      : t("hideAt", {
                          date: formatDateTime(
                            app.visibility_expires_at,
                            timezone,
                          ),
                        })}
                  </span>
                );
              })()}
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 bg-white dark:bg-gray-900 px-2 py-1 rounded text-xs font-mono break-all">
              {app.plaintext_secret}
            </code>
            <button
              onClick={() => onCopy(app.plaintext_secret!, `${copyKey}-secret`)}
              className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
              title={t("copyClientSecret")}
            >
              {isCopied(`${copyKey}-secret`) ? (
                <Check className="w-4 h-4 text-green-600" />
              ) : (
                <Copy className="w-4 h-4" />
              )}
            </button>
            <button
              onClick={() => onHide(app.client_id)}
              className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded"
              title={t("hideSecretNow")}
            >
              <EyeOff className="w-4 h-4" />
            </button>
          </div>
        </div>
      ) : (
        <div className="bg-gray-50 dark:bg-gray-800 p-3 rounded border border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2 text-gray-500 dark:text-gray-400">
            <EyeOff className="w-4 h-4" />
            <span className="text-sm">{t("secretHiddenOwner")}</span>
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-2">
        <ActionButton
          onClick={() => onEdit(app)}
          icon={<Pencil className="w-4 h-4" />}
        >
          {tCommon("edit")}
        </ActionButton>
        <ActionButton
          onClick={() => onRegenerate(app.client_id, app.client_name)}
          icon={<RefreshCw className="w-4 h-4" />}
        >
          {t("regenerate")}
        </ActionButton>
        <ActionButton
          onClick={() => onDelete(app.client_id)}
          variant="danger"
          icon={<Trash2 className="w-4 h-4" />}
        >
          {tCommon("delete")}
        </ActionButton>
      </div>

      {/* Metadata */}
      <div className="text-xs text-gray-500 dark:text-gray-400">
        <p>
          {t("created")}: {formatDateTime(app.created_at, timezone)}
        </p>
      </div>
    </div>
  );
}
