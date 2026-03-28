'use client';

/**
 * Dashboard Header
 *
 * Centered Kagura logo with language selector.
 * Issue #115: UI reorganization
 * Issue #160: Added link to homepage
 * Issue #194: Removed notification bell
 * Issue #221: Added language selector
 */

import Link from 'next/link';
import { cn, colors } from '@/styles/design-tokens';
import { KaguraLogo } from '@/components/icons/KaguraLogo';
import { LanguageSelector } from '@/components/LanguageSelector';

export function Header() {
  return (
    <header className={cn(
      'flex items-center justify-between h-16 px-6',
      'border-b',
      colors.border.default,
      colors.bg.card
    )}>
      {/* Spacer for centering */}
      <div className="w-20" />

      {/* Kagura Logo - Centered (links to workspace overview) */}
      <Link href="/workspace/dashboard" className="cursor-pointer hover:opacity-80 transition-opacity">
        <KaguraLogo className="h-10 w-auto" />
      </Link>

      {/* Language Selector - Right side */}
      <div className="w-20 flex justify-end">
        <LanguageSelector />
      </div>
    </header>
  );
}
