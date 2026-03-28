'use client';

import { useTranslations } from 'next-intl';
import {
  Check,
  FileText,
  X,
} from 'lucide-react';

export function DocFreeDev() {
  const t = useTranslations('landing');

  return (
    <section className="relative overflow-hidden bg-gradient-to-b from-gray-50 to-white px-4 py-24 sm:px-6 sm:py-32 lg:px-8 border-y border-gray-200">
      <div className="mx-auto max-w-5xl">
        <div className="text-center mb-16">
          <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-amber-100 to-orange-100 px-4 py-1.5 mb-4">
            <FileText className="h-4 w-4 text-amber-600" />
            <span className="text-sm font-semibold text-amber-700">{t('docFree.badge')}</span>
          </div>
          <h2 className="text-4xl md:text-5xl font-bold text-gray-900 mb-4">
            {t('docFree.title')}
          </h2>
          <p className="text-xl text-gray-600 max-w-3xl mx-auto">
            {t('docFree.subtitle')}
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
          {/* Before: Traditional Docs */}
          <div className="rounded-3xl border border-red-200 bg-red-50/50 p-8">
            <h3 className="text-lg font-bold text-red-800 mb-6 flex items-center gap-2">
              <X className="h-5 w-5 text-red-500" />
              {t('docFree.before')}
            </h3>
            <ul className="space-y-4">
              {(['beforeItem1', 'beforeItem2', 'beforeItem3', 'beforeItem4'] as const).map((key) => (
                <li key={key} className="flex items-start gap-3 text-red-700">
                  <span className="mt-1.5 h-1.5 w-1.5 rounded-full bg-red-400 flex-shrink-0" />
                  <span className="text-sm">{t(`docFree.${key}`)}</span>
                </li>
              ))}
            </ul>
          </div>

          {/* After: Kagura Memory */}
          <div className="rounded-3xl border border-green-200 bg-green-50/50 p-8">
            <h3 className="text-lg font-bold text-green-800 mb-6 flex items-center gap-2">
              <Check className="h-5 w-5 text-green-500" />
              {t('docFree.after')}
            </h3>
            <ul className="space-y-4">
              {(['afterItem1', 'afterItem2', 'afterItem3', 'afterItem4'] as const).map((key) => (
                <li key={key} className="flex items-start gap-3 text-green-700">
                  <Check className="h-4 w-4 text-green-500 flex-shrink-0 mt-0.5" />
                  <span className="text-sm font-medium">{t(`docFree.${key}`)}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </section>
  );
}
