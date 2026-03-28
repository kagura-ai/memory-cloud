'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import {
  ArrowRight,
  Check,
  Brain,
  Zap,
  Database,
  Network,
} from 'lucide-react';

export function NeuralMemory() {
  const t = useTranslations('landing');

  return (
    <section id="neural" className="relative overflow-hidden bg-gradient-to-b from-gray-50 to-white px-4 py-24 sm:px-6 sm:py-32 lg:px-8 border-y border-gray-200">
      <div className="mx-auto max-w-7xl">
        <div className="text-center mb-16">
          <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-purple-100 to-blue-100 px-4 py-1.5 mb-4">
            <Brain className="h-4 w-4 text-purple-600" />
            <span className="text-sm font-semibold text-purple-700">{t('neuralMemory')}</span>
          </div>
          <h2 className="text-4xl md:text-5xl font-bold text-gray-900 mb-4">
            {t('neuralMemoryTitle')}
          </h2>
          <p className="text-xl text-gray-600 max-w-3xl mx-auto">
            {t('neuralMemorySubtitle')}
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
          {/* Neural Feature 1: Hebbian Learning */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <div className="flex items-center gap-3 mb-4">
              <div className="rounded-xl bg-purple-600 p-2 text-white">
                <Brain className="h-5 w-5" />
              </div>
              <h3 className="text-xl font-bold text-gray-900">{t('hebbianLearning')}</h3>
            </div>
            <p className="text-gray-600 mb-4">
              {t('hebbianDesc')}
            </p>
            <ul className="space-y-2 text-sm text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('autoRelationship')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('connectionsStrengthen')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('zeroManual')}</span>
              </li>
            </ul>
          </div>

          {/* Neural Feature 2: Graph Network */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <div className="flex items-center gap-3 mb-4">
              <div className="rounded-xl bg-blue-600 p-2 text-white">
                <Network className="h-5 w-5" />
              </div>
              <h3 className="text-xl font-bold text-gray-900">{t('graphConnections')}</h3>
            </div>
            <p className="text-gray-600 mb-4">
              {t('graphConnectionsDesc')}
            </p>
            <ul className="space-y-2 text-sm text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('memoryNetwork')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('discoverRelated')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('connectionTracking')}</span>
              </li>
            </ul>
          </div>

          {/* Neural Feature 3: Vector Database */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <div className="flex items-center gap-3 mb-4">
              <div className="rounded-xl bg-green-600 p-2 text-white">
                <Database className="h-5 w-5" />
              </div>
              <h3 className="text-xl font-bold text-gray-900">{t('hybridVectorSearch')}</h3>
            </div>
            <p className="text-gray-600 mb-4">
              {t('hybridSearchDesc')}
            </p>
            <ul className="space-y-2 text-sm text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('qdrantVector')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('semanticBM25')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('aiReranking')}</span>
              </li>
            </ul>
          </div>

          {/* Neural Feature 4: Activation Spreading */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <div className="flex items-center gap-3 mb-4">
              <div className="rounded-xl bg-orange-600 p-2 text-white">
                <Zap className="h-5 w-5" />
              </div>
              <h3 className="text-xl font-bold text-gray-900">{t('activationSpreading')}</h3>
            </div>
            <p className="text-gray-600 mb-4">
              {t('activationSpreadingDesc')}
            </p>
            <ul className="space-y-2 text-sm text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('autoDiscover')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('serendipitous')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('smarterRecall')}</span>
              </li>
            </ul>
          </div>
        </div>

        {/* Architecture Diagram or CTA */}
        <div className="mt-16 text-center">
          <p className="text-gray-600 mb-6 max-w-2xl mx-auto">
            {t('uniqueCombination')}
          </p>
          <Button size="lg" variant="default" asChild>
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
