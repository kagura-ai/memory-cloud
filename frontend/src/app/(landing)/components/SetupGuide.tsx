'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import {
  ArrowRight,
  Terminal,
  Brain,
  MessageSquare,
} from 'lucide-react';

export function SetupGuide() {
  const t = useTranslations('landing');

  return (
    <section id="setup" className="relative overflow-hidden bg-white px-4 py-24 sm:px-6 sm:py-32 lg:px-8 border-y border-gray-200">
      <div className="mx-auto max-w-7xl">
        <div className="text-center mb-16">
          <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-brand-green-100 to-emerald-100 px-4 py-1.5 mb-4">
            <Terminal className="h-4 w-4 text-brand-green-600" />
            <span className="text-sm font-semibold text-brand-green-700">{t('setupGuide')}</span>
          </div>
          <h2 className="text-4xl md:text-5xl font-bold text-gray-900 mb-4">
            {t('simpleSetup')}
          </h2>
          <p className="text-xl text-gray-600 max-w-3xl mx-auto">
            {t('setupSubtitle')}
          </p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-12">
          {/* Claude Setup */}
          <div className="space-y-6">
            <div className="flex items-center gap-3 mb-6">
              <div className="rounded-xl bg-gradient-to-br from-purple-600 to-pink-600 p-3 text-white shadow-lg">
                <Brain className="h-6 w-6" />
              </div>
              <h3 className="text-2xl font-bold text-gray-900">{t('claudeSetup')}</h3>
            </div>

            <div className="rounded-2xl border border-gray-200 bg-gray-50 p-6">
              <p className="text-sm font-semibold text-gray-700 mb-4">{t('step1')}: {t('createOAuthApp')}</p>
              <ol className="space-y-3 text-gray-700">
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">1</span>
                  <span>{t('setupStep1_1')}</span>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">2</span>
                  <span>{t('setupStep1_2')}</span>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">3</span>
                  <span>{t('setupStep1_3')}</span>
                </li>
              </ol>
            </div>

            <div className="rounded-2xl border border-gray-200 bg-gray-50 p-6">
              <p className="text-sm font-semibold text-gray-700 mb-4">{t('step2')}: {t('configureClaude')}</p>
              <div className="bg-gray-900 rounded-lg p-4 overflow-x-auto">
                <pre className="text-xs text-green-400 font-mono">
{`// Create .mcp.json in your project root
{
  "mcpServers": {
    "kagura": {
      "url": "https://your-domain.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}`}
                </pre>
              </div>
              <p className="text-xs text-gray-600 mt-3">
                {'\u{1F4A1}'} {t('replaceApiKey')}
              </p>
            </div>
          </div>

          {/* ChatGPT Setup */}
          <div className="space-y-6">
            <div className="flex items-center gap-3 mb-6">
              <div className="rounded-xl bg-gradient-to-br from-green-600 to-emerald-600 p-3 text-white shadow-lg">
                <MessageSquare className="h-6 w-6" />
              </div>
              <h3 className="text-2xl font-bold text-gray-900">{t('chatgptSetup')}</h3>
            </div>

            <div className="rounded-2xl border border-gray-200 bg-gray-50 p-6">
              <p className="text-sm font-semibold text-gray-700 mb-4">{t('step1')}: {t('createApiKey')}</p>
              <ol className="space-y-3 text-gray-700">
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">1</span>
                  <span>{t('setupStep2_1')}</span>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">2</span>
                  <span>{t('setupStep2_2')}</span>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">3</span>
                  <span>{t('setupStep2_3')}</span>
                </li>
              </ol>
            </div>

            <div className="rounded-2xl border border-gray-200 bg-gray-50 p-6">
              <p className="text-sm font-semibold text-gray-700 mb-4">{t('step2')}: {t('addToChatGPT')}</p>
              <ol className="space-y-3 text-gray-700">
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">1</span>
                  <span>{t('chatgptStep1')}</span>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">2</span>
                  <span>{t('chatgptStep2')}</span>
                </li>
                <li className="flex gap-3">
                  <span className="flex-shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-brand-green-600 text-white text-xs font-bold">3</span>
                  <span>{t('chatgptStep3')}</span>
                </li>
              </ol>
            </div>
          </div>
        </div>

        {/* Quick Links */}
        <div className="mt-16 text-center">
          <Button size="lg" asChild>
            <a href="/login">
              {t('startSetupNow')}
              <ArrowRight className="ml-2 h-5 w-5" />
            </a>
          </Button>
        </div>
      </div>
    </section>
  );
}
