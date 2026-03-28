'use client';

/**
 * Instructions Template Component
 *
 * Issue #215: Provides copyable Client Instructions for AI clients (ChatGPT, Claude)
 * that don't support MCP server initialize instructions.
 * Issue #223: Added i18n support
 */

import { useState } from 'react';
import { ChevronDown, Copy, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/styles/design-tokens';
import { useTranslations } from 'next-intl';

interface InstructionsTemplateProps {
  contextName: string;
  usageGuide: string | null;
  isPrivate: boolean;
}

/**
 * Generate context-specific Client Instructions template
 */
function generateTemplate(
  contextName: string,
  usageGuide: string | null,
  isPrivate: boolean,
  t: (key: string) => string
): string {
  const privacyNote = isPrivate
    ? t('privateContextNote')
    : t('sharedContextNote');

  const usageSection = usageGuide
    ? usageGuide
    : t('noGuidelinesSet');

  return `# Kagura Memory Cloud Instructions

You have access to Kagura Memory Cloud MCP tools for persistent memory.

## Current Context: "${contextName}"
${privacyNote}

## Quick Start
1. Call get_context_info() at session start to load guidelines
2. Follow context.usage_guide for this context's rules
3. Use recall() before starting new tasks to check existing knowledge

## Core Workflow
- recall() - Search before starting tasks
- remember() - Store important decisions/code
- explore() - Find related memories via graph traversal

## remember() Tips
- summary: Write reusable conclusions (not process)
- importance: 0.9+ critical, 0.6-0.8 useful, 0.3-0.5 reference
- tags: Include project/domain tags for filtering

## recall() Tips
- Use HyDE: Generate hypothetical answer, then search with it
- Expand queries with related terms
- Use filters: {"type": "decision"}, {"tags": ["project:x"]}

## Context-Specific Guidelines
${usageSection}

## Security
Never store: passwords, API keys, PII, secrets`;
}

export function InstructionsTemplate({
  contextName,
  usageGuide,
  isPrivate,
}: InstructionsTemplateProps) {
  const t = useTranslations('contexts');
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);

  const template = generateTemplate(contextName, usageGuide, isPrivate, t);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(template);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  return (
    <div className="border-t border-slate-200 dark:border-slate-700 mt-3 pt-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center justify-between w-full text-left text-sm text-slate-600 dark:text-slate-400 hover:text-slate-800 dark:hover:text-slate-200 transition-colors"
      >
        <span className="font-medium">{t('aiClientInstructions')}</span>
        <ChevronDown
          className={cn(
            'h-4 w-4 transition-transform duration-200',
            expanded && 'rotate-180'
          )}
        />
      </button>

      {expanded && (
        <div className="mt-3 space-y-2">
          <p className="text-xs text-slate-500 dark:text-slate-400">
            {t('instructionsDescription')}
          </p>

          <div className="relative">
            <pre className="text-xs bg-slate-100 dark:bg-slate-900 p-3 rounded-lg overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap">
              {template}
            </pre>

            <Button
              variant="outline"
              size="sm"
              onClick={handleCopy}
              className="absolute top-2 right-2 h-7 px-2"
            >
              {copied ? (
                <>
                  <Check className="h-3 w-3 mr-1 text-green-600" />
                  <span className="text-xs">{t('copied')}</span>
                </>
              ) : (
                <>
                  <Copy className="h-3 w-3 mr-1" />
                  <span className="text-xs">{t('copy')}</span>
                </>
              )}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
