'use client';

import { useTranslations } from 'next-intl';
import {
  Brain,
  Globe,
  Shield,
  Code2,
  Activity,
  Users,
  Sparkles,
} from 'lucide-react';

export function FeaturesGrid() {
  const t = useTranslations('landing');

  return (
    <section id="features" className="relative overflow-hidden bg-gradient-to-b from-gray-50 to-white px-4 py-24 sm:px-6 sm:py-32 lg:px-8">
      <div className="mx-auto max-w-7xl">
        <div className="mb-16 text-center">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-brand-green-100 to-emerald-100 px-4 py-1.5 text-sm font-semibold text-brand-green-700">
            <Sparkles className="h-4 w-4" />
            <span>Features</span>
          </div>
          <h2 className="mb-4 text-4xl font-bold text-gray-900 sm:text-5xl">{t('featuresTitle')}</h2>
          <p className="mx-auto max-w-2xl text-xl text-gray-600">
            {t('featuresSubtitle')}
          </p>
        </div>

        <div className="grid grid-cols-1 gap-6 md:grid-cols-2 lg:grid-cols-3">
          {[
            {
              icon: Brain,
              title: t('completeMCPToolkit'),
              description: t('mcpToolkitDesc'),
              gradient: 'from-blue-500 to-cyan-500',
              stats: t('allTools'),
            },
            {
              icon: Globe,
              title: t('neuralMemory'),
              description: t('neuralMemoryDesc'),
              gradient: 'from-purple-500 to-pink-500',
              stats: t('aiPowered'),
            },
            {
              icon: Users,
              title: t('workspaces'),
              description: t('workspacesDesc'),
              gradient: 'from-yellow-500 to-orange-500',
              stats: t('multiTenant'),
            },
            {
              icon: Shield,
              title: t('adminQuotas'),
              description: t('adminQuotasDesc'),
              gradient: 'from-green-500 to-emerald-500',
              stats: t('configurable'),
            },
            {
              icon: Code2,
              title: t('easyIntegration'),
              description: t('easyIntegrationDesc'),
              gradient: 'from-red-500 to-pink-500',
              stats: t('fiveMinSetup'),
            },
            {
              icon: Activity,
              title: t('realtimeAnalytics'),
              description: t('realtimeAnalyticsDesc'),
              gradient: 'from-indigo-500 to-purple-500',
              stats: t('liveMetrics'),
            },
          ].map((feature, index) => {
            const Icon = feature.icon;
            return (
              <div
                key={feature.title}
                className="group relative overflow-hidden rounded-3xl border border-gray-200 bg-white p-8 transition-all duration-500 hover:-translate-y-1 hover:border-transparent hover:shadow-2xl"
                style={{ animationDelay: `${index * 100}ms` }}
              >
                <div className={`pointer-events-none absolute inset-0 bg-gradient-to-br ${feature.gradient} opacity-0 transition-opacity duration-500 group-hover:opacity-5`} />

                <div className="relative">
                  <div className="mb-6 flex items-center justify-between">
                    <div className={`inline-flex rounded-2xl bg-gradient-to-br ${feature.gradient} p-3 text-white shadow-lg transition-transform group-hover:scale-110`}>
                      <Icon className="h-6 w-6" />
                    </div>
                    <div className={`rounded-full bg-gradient-to-br ${feature.gradient} bg-opacity-10 px-3 py-1 text-xs font-bold`}>
                      {feature.stats}
                    </div>
                  </div>

                  <h3 className="mb-3 text-lg font-bold text-gray-900">{feature.title}</h3>
                  <p className="text-gray-600">{feature.description}</p>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
