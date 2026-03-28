'use client';

/**
 * Global Language Selector Component
 * Issue #221: Language selection for i18n support
 *
 * Features:
 * - Dropdown menu with language options
 * - Syncs with localStorage for unauthenticated users
 * - Syncs with User Profile API for authenticated users
 * - Triggers page reload to apply changes
 */

import { useState, useTransition } from 'react';
import { useLocale, locales, localeNames, localeFlags, type Locale } from '@/i18n';
import { useAuth } from '@/contexts/AuthContext';
import { updateUserProfile } from '@/lib/api/base';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Button } from '@/components/ui/button';
import { Globe, Check } from 'lucide-react';
import { toast } from 'sonner';

interface LanguageSelectorProps {
  /** Show full language name instead of just flag/icon */
  showLabel?: boolean;
  /** Compact mode for sidebars */
  compact?: boolean;
  /** Additional CSS classes */
  className?: string;
}

export function LanguageSelector({
  showLabel = false,
  compact = false,
  className = '',
}: LanguageSelectorProps) {
  const { locale, setLocale } = useLocale();
  const { user, isAuthenticated } = useAuth();
  const [isPending, startTransition] = useTransition();
  const [isOpen, setIsOpen] = useState(false);

  const handleLocaleChange = async (newLocale: Locale) => {
    if (newLocale === locale) {
      setIsOpen(false);
      return;
    }

    startTransition(async () => {
      try {
        // Update localStorage immediately
        setLocale(newLocale);

        // If authenticated, also update user profile
        if (isAuthenticated && user) {
          try {
            await updateUserProfile({ locale: newLocale });
          } catch (error) {
            console.error('Failed to update user locale:', error);
            // Don't show error toast - localStorage update still succeeded
          }
        }

        // Close dropdown and reload to apply changes
        setIsOpen(false);
        toast.success(
          newLocale === 'ja' ? '言語を変更しました' : 'Language changed'
        );

        // Reload page to apply new locale
        window.location.reload();
      } catch (error) {
        console.error('Failed to change locale:', error);
        toast.error(
          locale === 'ja' ? 'エラーが発生しました' : 'An error occurred'
        );
      }
    });
  };

  return (
    <DropdownMenu open={isOpen} onOpenChange={setIsOpen}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size={compact ? 'sm' : 'default'}
          className={`gap-2 ${className}`}
          disabled={isPending}
        >
          <Globe className="h-4 w-4" />
          {showLabel && (
            <span className="hidden sm:inline">
              {localeFlags[locale]} {localeNames[locale]}
            </span>
          )}
          {!showLabel && !compact && (
            <span className="text-xs">{localeFlags[locale]}</span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-40">
        {locales.map((loc) => (
          <DropdownMenuItem
            key={loc}
            onClick={() => handleLocaleChange(loc)}
            className="flex items-center justify-between cursor-pointer"
            disabled={isPending}
          >
            <span className="flex items-center gap-2">
              <span>{localeFlags[loc]}</span>
              <span>{localeNames[loc]}</span>
            </span>
            {locale === loc && <Check className="h-4 w-4 text-emerald-600" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
