'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import {
  ArrowRight,
  Globe,
  Shield,
  Code2,
  Database,
  Layers,
} from 'lucide-react';

export function PublicContexts() {
  const t = useTranslations('landing');

  return (
    <section className="relative py-16 bg-white border-y border-gray-200">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="text-center mb-12">
          <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-purple-100 to-pink-100 px-4 py-1.5 mb-4">
            <Globe className="h-4 w-4 text-purple-600" />
            <span className="text-sm font-semibold text-purple-700">Knowledge Sharing</span>
          </div>
          <h2 className="text-4xl font-bold text-gray-900 mb-4">{t('publicContexts.title')}</h2>
          <p className="text-xl text-gray-600 max-w-3xl mx-auto">
            {t('publicContexts.description')}
          </p>
        </div>

        <div className="grid md:grid-cols-2 lg:grid-cols-4 gap-6">
          <div className="group p-6 bg-gradient-to-br from-purple-50 to-white rounded-2xl border border-purple-200 hover:border-purple-400 hover:shadow-xl transition-all">
            <div className="rounded-xl bg-purple-600 p-3 text-white inline-flex mb-4">
              <Database className="h-6 w-6" />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">{t('publicContexts.feature1')}</h3>
            <p className="text-sm text-gray-600">
              Share indexed data across your workspace with full-text and semantic search capabilities
            </p>
          </div>

          <div className="group p-6 bg-gradient-to-br from-blue-50 to-white rounded-2xl border border-blue-200 hover:border-blue-400 hover:shadow-xl transition-all">
            <div className="rounded-xl bg-blue-600 p-3 text-white inline-flex mb-4">
              <Code2 className="h-6 w-6" />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">{t('publicContexts.feature2')}</h3>
            <p className="text-sm text-gray-600">
              Enable external applications to search your knowledge base via REST API endpoints
            </p>
          </div>

          <div className="group p-6 bg-gradient-to-br from-green-50 to-white rounded-2xl border border-green-200 hover:border-green-400 hover:shadow-xl transition-all">
            <div className="rounded-xl bg-green-600 p-3 text-white inline-flex mb-4">
              <Layers className="h-6 w-6" />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">{t('publicContexts.feature3')}</h3>
            <p className="text-sm text-gray-600">
              Define field metadata and index hints for precise search and filtering capabilities
            </p>
          </div>

          <div className="group p-6 bg-gradient-to-br from-orange-50 to-white rounded-2xl border border-orange-200 hover:border-orange-400 hover:shadow-xl transition-all">
            <div className="rounded-xl bg-orange-600 p-3 text-white inline-flex mb-4">
              <Shield className="h-6 w-6" />
            </div>
            <h3 className="text-lg font-bold text-gray-900 mb-2">{t('publicContexts.feature4')}</h3>
            <p className="text-sm text-gray-600">
              Secure access with workspace-scoped tokens and role-based permissions
            </p>
          </div>
        </div>

        <div className="mt-12 text-center">
          <Button size="lg" asChild>
            <a href="/login">
              Start Using Public Contexts
              <ArrowRight className="ml-2 h-5 w-5" />
            </a>
          </Button>
        </div>
      </div>
    </section>
  );
}
