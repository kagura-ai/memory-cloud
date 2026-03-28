'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import {
  ArrowRight,
  Check,
  Terminal,
  MessageSquare,
  Brain,
  Sparkles,
  Users,
} from 'lucide-react';

export function UseCases() {
  const t = useTranslations('landing');

  return (
    <section id="use-cases" className="relative overflow-hidden bg-gradient-to-b from-white to-gray-50 px-4 py-24 sm:px-6 sm:py-32 lg:px-8 border-y border-gray-200">
      <div className="mx-auto max-w-7xl">
        <div className="text-center mb-16">
          <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-blue-100 to-cyan-100 px-4 py-1.5 mb-4">
            <Sparkles className="h-4 w-4 text-blue-600" />
            <span className="text-sm font-semibold text-blue-700">{t('useCases')}</span>
          </div>
          <h2 className="text-4xl md:text-5xl font-bold text-gray-900 mb-4">
            {t('perfectForAnyWorkflow')}
          </h2>
          <p className="text-xl text-gray-600 max-w-3xl mx-auto">
            {t('useCasesSubtitle')}
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
          {/* Use Case 1: Coding */}
          <div className="rounded-3xl border border-gray-200 bg-white p-6 hover:shadow-xl transition-all">
            <div className="rounded-xl bg-gradient-to-br from-blue-500 to-cyan-500 p-3 text-white inline-flex mb-4">
              <Terminal className="h-6 w-6" />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">{t('codingDev')}</h3>
            <p className="text-sm text-gray-600 mb-3">
              {t('codingDevDesc')}
            </p>
            <ul className="space-y-1.5 text-xs text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('apiDocs')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('codePatterns')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('debugSolutions')}</span>
              </li>
            </ul>
          </div>

          {/* Use Case 2: Research */}
          <div className="rounded-3xl border border-gray-200 bg-white p-6 hover:shadow-xl transition-all">
            <div className="rounded-xl bg-gradient-to-br from-purple-500 to-pink-500 p-3 text-white inline-flex mb-4">
              <Brain className="h-6 w-6" />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">{t('researchLearning')}</h3>
            <p className="text-sm text-gray-600 mb-3">
              {t('researchDesc')}
            </p>
            <ul className="space-y-1.5 text-xs text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('paperSummaries')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('learningNotes')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('ideaConnections')}</span>
              </li>
            </ul>
          </div>

          {/* Use Case 3: Content Creation */}
          <div className="rounded-3xl border border-gray-200 bg-white p-6 hover:shadow-xl transition-all">
            <div className="rounded-xl bg-gradient-to-br from-green-500 to-emerald-500 p-3 text-white inline-flex mb-4">
              <MessageSquare className="h-6 w-6" />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">{t('writingContent')}</h3>
            <p className="text-sm text-gray-600 mb-3">
              {t('writingDesc')}
            </p>
            <ul className="space-y-1.5 text-xs text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('articleDrafts')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('researchNotes')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('creativeIdeas')}</span>
              </li>
            </ul>
          </div>

          {/* Use Case 4: Team Projects */}
          <div className="rounded-3xl border border-gray-200 bg-white p-6 hover:shadow-xl transition-all">
            <div className="rounded-xl bg-gradient-to-br from-orange-500 to-red-500 p-3 text-white inline-flex mb-4">
              <Users className="h-6 w-6" />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">{t('teamCollab')}</h3>
            <p className="text-sm text-gray-600 mb-3">
              {t('teamCollabDesc')}
            </p>
            <ul className="space-y-1.5 text-xs text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('projectDocs')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('meetingNotes')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-3 w-3 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('sharedDecisions')}</span>
              </li>
            </ul>
          </div>
        </div>

        {/* CTA */}
        <div className="mt-16 text-center">
          <p className="text-gray-600 mb-6">
            {t('useCaseClosing')}
          </p>
          <Button size="lg" asChild>
            <a href="/login">
              {t('getStarted')}
              <ArrowRight className="ml-2 h-5 w-5" />
            </a>
          </Button>
        </div>
      </div>
    </section>
  );
}
