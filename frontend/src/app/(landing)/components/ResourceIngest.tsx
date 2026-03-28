'use client';

import { useTranslations } from 'next-intl';
import {
  Check,
  Database,
} from 'lucide-react';

export function ResourceIngest() {
  const t = useTranslations('landing');

  return (
    <section className="relative py-16 bg-gradient-to-b from-gray-50 to-white border-t border-gray-200">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="text-center mb-12">
          <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-blue-100 to-cyan-100 px-4 py-1.5 mb-4">
            <Database className="h-4 w-4 text-blue-600" />
            <span className="text-sm font-semibold text-blue-700">Enterprise Data Integration</span>
          </div>
          <h2 className="text-4xl font-bold text-gray-900 mb-4">{t('resourceIngest.title')}</h2>
          <p className="text-xl text-gray-600 max-w-3xl mx-auto">
            {t('resourceIngest.description')}
          </p>
        </div>

        <div className="grid lg:grid-cols-2 gap-12 items-center">
          <div>
            <div className="space-y-4">
              <div className="flex items-start gap-3 p-4 bg-white rounded-lg border border-gray-200 hover:border-brand-green-500 hover:shadow-lg transition-all">
                <div className="rounded-lg bg-brand-green-100 p-2">
                  <Check className="h-5 w-5 text-brand-green-600" />
                </div>
                <div>
                  <p className="font-semibold text-gray-900">{t('resourceIngest.feature1')}</p>
                  <p className="text-sm text-gray-600 mt-1">Keep your knowledge base in sync with real-time updates</p>
                </div>
              </div>
              <div className="flex items-start gap-3 p-4 bg-white rounded-lg border border-gray-200 hover:border-brand-green-500 hover:shadow-lg transition-all">
                <div className="rounded-lg bg-purple-100 p-2">
                  <Check className="h-5 w-5 text-purple-600" />
                </div>
                <div>
                  <p className="font-semibold text-gray-900">{t('resourceIngest.feature2')}</p>
                  <p className="text-sm text-gray-600 mt-1">Define field types and index hints for optimal search</p>
                </div>
              </div>
              <div className="flex items-start gap-3 p-4 bg-white rounded-lg border border-gray-200 hover:border-brand-green-500 hover:shadow-lg transition-all">
                <div className="rounded-lg bg-blue-100 p-2">
                  <Check className="h-5 w-5 text-blue-600" />
                </div>
                <div>
                  <p className="font-semibold text-gray-900">{t('resourceIngest.feature3')}</p>
                  <p className="text-sm text-gray-600 mt-1">Make ingested data searchable across your team</p>
                </div>
              </div>
            </div>
          </div>
          <div className="relative rounded-xl border border-gray-300 bg-gray-900 p-6 shadow-2xl">
            <div className="absolute -top-3 left-4 bg-gray-900 px-2">
              <span className="text-xs font-semibold text-gray-400">API Example</span>
            </div>
            <pre className="text-xs text-gray-300 overflow-x-auto">
{`POST /api/v1/resources/products/events
X-Resource-API-Key: kagura_resource_xxx

{
  "op": "upsert",
  "doc_id": "prod_12345",
  "version": 1,
  "payload": {
    "name": "Product Name",
    "price": 9800,
    "stock": 50
  }
}`}
            </pre>
          </div>
        </div>
      </div>
    </section>
  );
}
