/**
 * Credentials Status Component
 *
 * Migration 034: Display API Key and OAuth App status for members
 *
 * Shows:
 * - API Key status (🟢 Visible / 🔴 Hidden, count)
 * - OAuth Apps status (Claude, ChatGPT, Custom)
 * - Manage link to credential detail page
 */

'use client';

import Link from 'next/link';
import { Settings } from 'lucide-react';

interface CredentialsStatusProps {
  userId: string;
  currentUserId?: string;  // Current logged-in user ID
  apiKeyStatus?: {
    count: number;
    visible: boolean;
  };
  oauthApps?: {
    claude: { visible: boolean } | null;
    chatgpt: { visible: boolean } | null;
    customCount: number;
  };
}

export function CredentialsStatus({
  userId,
  currentUserId,
  apiKeyStatus,
  oauthApps,
}: CredentialsStatusProps) {
  // Only show credentials status for self
  const isCurrentUser = userId === currentUserId;

  if (!isCurrentUser) {
    return (
      <div className="flex items-center gap-3">
        <span className="text-gray-400 text-sm">—</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      {/* API Key Status */}
      <div className="flex items-center gap-1.5">
        <span className="text-xs font-medium text-gray-600 dark:text-gray-400 w-12">
          API:
        </span>
        {apiKeyStatus && apiKeyStatus.count > 0 ? (
          <>
            <span className={apiKeyStatus.visible ? 'text-green-600' : 'text-red-600'}>
              {apiKeyStatus.visible ? '🟢' : '🔴'}
            </span>
            {apiKeyStatus.count > 1 && (
              <span className="text-xs text-gray-500">
                ×{apiKeyStatus.count}
              </span>
            )}
          </>
        ) : (
          <span className="text-gray-400 text-xs">—</span>
        )}
      </div>

      {/* OAuth Apps Status - Compact */}
      <div className="flex items-center gap-2 text-xs">
        {/* Claude */}
        <div className="flex items-center gap-1">
          <span className="font-medium text-gray-600 dark:text-gray-400 w-12">
            Claude:
          </span>
          {oauthApps?.claude ? (
            <span className={oauthApps.claude.visible ? 'text-green-600' : 'text-red-600'}>
              {oauthApps.claude.visible ? '🟢' : '🔴'}
            </span>
          ) : (
            <span className="text-gray-400">—</span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 text-xs">
        {/* ChatGPT */}
        <div className="flex items-center gap-1">
          <span className="font-medium text-gray-600 dark:text-gray-400 w-12">
            GPT:
          </span>
          {oauthApps?.chatgpt ? (
            <span className={oauthApps.chatgpt.visible ? 'text-green-600' : 'text-red-600'}>
              {oauthApps.chatgpt.visible ? '🟢' : '🔴'}
            </span>
          ) : (
            <span className="text-gray-400">—</span>
          )}
        </div>

        {/* Custom - Compact */}
        {oauthApps?.customCount && oauthApps.customCount > 0 && (
          <div className="flex items-center gap-0.5 ml-2">
            <span>🔧</span>
            <span className="text-blue-600 font-semibold text-xs">
              {oauthApps.customCount}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
