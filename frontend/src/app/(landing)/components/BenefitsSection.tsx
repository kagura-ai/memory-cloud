'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import {
  ArrowRight,
  Check,
  Zap,
  Brain,
  Users,
  Sparkles,
} from 'lucide-react';

export function BenefitsSection() {
  const t = useTranslations('landing');

  return (
    <section className="relative overflow-hidden border-y border-gray-200 bg-gradient-to-b from-white to-gray-50 py-24">
      <div className="mx-auto max-w-7xl px-6 lg:px-8">
        {/* Section Header */}
        <div className="text-center mb-16">
          <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-brand-green-100 to-emerald-100 px-4 py-1.5 mb-4">
            <Sparkles className="h-4 w-4 text-brand-green-600" />
            <span className="text-sm font-semibold text-brand-green-700">{t('whyKagura')}</span>
          </div>
          <h2 className="text-4xl md:text-5xl font-bold text-gray-900 mb-4">
            {t('benefitsTitle')}
          </h2>
          <p className="text-xl text-gray-600 max-w-3xl mx-auto">
            {t('benefitsSubtitle')}
          </p>
        </div>

        {/* 3-Column Benefits Grid */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-8">
          {/* Benefit 1: Easy Setup for Claude & ChatGPT */}
          <div className="group rounded-3xl border border-gray-200 bg-white p-8 shadow-sm transition-all hover:-translate-y-2 hover:shadow-2xl">
            <div className="inline-flex rounded-2xl bg-gradient-to-br from-brand-green-600 to-emerald-600 p-3 text-white shadow-lg">
              <Zap className="h-6 w-6" />
            </div>
            <h3 className="mt-6 text-2xl font-bold text-gray-900">
              {t('benefit1Title')}
            </h3>
            <p className="mt-4 text-gray-600">
              {t('benefit1Desc')}
            </p>
            <ul className="mt-6 space-y-3">
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit1Feature1')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit1Feature2')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit1Feature3')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit1Feature4')}</span>
              </li>
            </ul>
            <div className="mt-8">
              <Button variant="outline" className="w-full group/btn" asChild>
                <a href="#setup">
                  {t('setupGuide')}
                  <ArrowRight className="ml-2 h-4 w-4 transition-transform group-hover/btn:translate-x-1" />
                </a>
              </Button>
            </div>
          </div>

          {/* Benefit 2: Neural Memory Architecture */}
          <div className="group rounded-3xl border border-gray-200 bg-white p-8 shadow-sm transition-all hover:-translate-y-2 hover:shadow-2xl">
            <div className="inline-flex rounded-2xl bg-gradient-to-br from-purple-600 to-blue-600 p-3 text-white shadow-lg">
              <Brain className="h-6 w-6" />
            </div>
            <h3 className="mt-6 text-2xl font-bold text-gray-900">
              {t('benefit2Title')}
            </h3>
            <p className="mt-4 text-gray-600">
              {t('benefit2Desc')}
            </p>
            <ul className="mt-6 space-y-3">
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit2Feature1')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit2Feature2')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit2Feature3')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit2Feature4')}</span>
              </li>
            </ul>
            <div className="mt-8">
              <Button variant="outline" className="w-full group/btn" asChild>
                <a href="#neural">
                  {t('howItWorks')}
                  <ArrowRight className="ml-2 h-4 w-4 transition-transform group-hover/btn:translate-x-1" />
                </a>
              </Button>
            </div>
          </div>

          {/* Benefit 3: Team Context Sharing */}
          <div className="group rounded-3xl border border-gray-200 bg-white p-8 shadow-sm transition-all hover:-translate-y-2 hover:shadow-2xl">
            <div className="inline-flex rounded-2xl bg-gradient-to-br from-orange-600 to-red-600 p-3 text-white shadow-lg">
              <Users className="h-6 w-6" />
            </div>
            <h3 className="mt-6 text-2xl font-bold text-gray-900">
              {t('benefit3Title')}
            </h3>
            <p className="mt-4 text-gray-600">
              {t('benefit3Desc')}
            </p>
            <ul className="mt-6 space-y-3">
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit3Feature1')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit3Feature2')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit3Feature3')}</span>
              </li>
              <li className="flex items-start gap-3 text-gray-700">
                <Check className="h-5 w-5 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('benefit3Feature4')}</span>
              </li>
            </ul>
            <div className="mt-8">
              <Button variant="outline" className="w-full group/btn" asChild>
                <a href="#team">
                  {t('seeTeamFeatures')}
                  <ArrowRight className="ml-2 h-4 w-4 transition-transform group-hover/btn:translate-x-1" />
                </a>
              </Button>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
