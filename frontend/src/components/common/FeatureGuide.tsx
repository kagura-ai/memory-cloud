'use client';

/**
 * FeatureGuide - Collapsible feature description section
 *
 * Issue #319: Add feature description sections to Integration pages
 *
 * Features:
 * - Collapsible with localStorage persistence
 * - Open by default on first visit
 * - Consistent styling across all integration pages
 */

import { useState, useEffect } from 'react';
import { ChevronDown } from 'lucide-react';

interface FeatureGuideProps {
  storageKey: string;
  title: string;
  children: React.ReactNode;
}

export function FeatureGuide({ storageKey, title, children }: FeatureGuideProps) {
  const [isOpen, setIsOpen] = useState(true);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem(`feature-guide:${storageKey}`);
    if (stored !== null) {
      setIsOpen(stored === 'open');
    }
    setMounted(true);
  }, [storageKey]);

  const toggle = () => {
    const next = !isOpen;
    setIsOpen(next);
    localStorage.setItem(`feature-guide:${storageKey}`, next ? 'open' : 'closed');
  };

  if (!mounted) return null;

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 overflow-hidden">
      <button
        onClick={toggle}
        className="w-full flex items-center justify-between px-4 py-3 bg-gray-50 dark:bg-gray-800/50 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors text-left"
      >
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {title}
        </span>
        <ChevronDown
          className={`h-4 w-4 text-gray-500 dark:text-gray-400 transition-transform duration-200 ${
            isOpen ? 'rotate-180' : ''
          }`}
        />
      </button>
      {isOpen && (
        <div className="px-4 py-3 bg-gray-50/50 dark:bg-gray-800/30 space-y-3 text-sm text-gray-700 dark:text-gray-300">
          {children}
        </div>
      )}
    </div>
  );
}
