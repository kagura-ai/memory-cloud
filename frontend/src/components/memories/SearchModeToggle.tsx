/**
 * Search Mode Toggle Component
 *
 * Allows users to switch between different memory search modes:
 * - Simple: Basic query search
 * - Semantic: RAG/vector search (by meaning)
 * - Keyword: BM25 search (by exact terms)
 * - Timeline: Time-based search
 */

import { Button } from '@/components/ui/button';
import { Brain, Search, Calendar, Sparkles } from 'lucide-react';

export type SearchMode = 'simple' | 'semantic' | 'keyword' | 'timeline';

interface SearchModeToggleProps {
  value: SearchMode;
  onChange: (mode: SearchMode) => void;
}

export function SearchModeToggle({ value, onChange }: SearchModeToggleProps) {
  const modes: { id: SearchMode; label: string; icon: typeof Search; description: string }[] = [
    {
      id: 'simple',
      label: 'Simple',
      icon: Search,
      description: 'Basic search',
    },
    {
      id: 'semantic',
      label: 'Semantic',
      icon: Brain,
      description: 'Search by meaning (best for concepts)',
    },
    {
      id: 'keyword',
      label: 'Keyword',
      icon: Sparkles,
      description: 'Search by exact terms (best for names)',
    },
    {
      id: 'timeline',
      label: 'Timeline',
      icon: Calendar,
      description: 'Search by date',
    },
  ];

  return (
    <div className="inline-flex gap-1 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 p-1">
      {modes.map((mode) => {
        const Icon = mode.icon;
        const isSelected = value === mode.id;
        return (
          <Button
            key={mode.id}
            size="sm"
            variant={isSelected ? 'default' : 'ghost'}
            onClick={() => onChange(mode.id)}
            className={`gap-2 transition-all ${
              isSelected
                ? 'bg-gradient-to-r from-brand-green-600 to-emerald-600 text-white hover:from-brand-green-700 hover:to-emerald-700'
                : 'hover:bg-gray-100 dark:hover:bg-gray-700'
            }`}
            title={mode.description}
          >
            <Icon className="h-4 w-4" />
            <span className="hidden sm:inline">{mode.label}</span>
          </Button>
        );
      })}
    </div>
  );
}
