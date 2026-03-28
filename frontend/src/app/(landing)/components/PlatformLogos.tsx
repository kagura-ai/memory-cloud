'use client';

import { useTranslations } from 'next-intl';
import { Users } from 'lucide-react';

export function PlatformLogos() {
  const t = useTranslations('landing');

  return (
    <section className="border-y border-gray-200/50 bg-gradient-to-b from-gray-50/50 to-white py-16">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="mb-10 flex items-center justify-center gap-3">
          <Users className="h-5 w-5 text-gray-400" />
          <p className="text-center text-sm font-semibold uppercase tracking-wider text-gray-500">
            {t('platformsText')}
          </p>
        </div>

        <div className="grid grid-cols-2 gap-6 md:grid-cols-4">
          {[
            { name: 'Claude', icon: '\u{1F916}', gradient: 'from-purple-500 to-pink-500' },
            { name: 'ChatGPT', icon: '\u{1F4AC}', gradient: 'from-green-500 to-emerald-500' },
            { name: 'Gemini', icon: '\u2728', gradient: 'from-blue-500 to-cyan-500' },
            { name: 'MCP Protocol', icon: '\u{1F50C}', gradient: 'from-orange-500 to-red-500' },
          ].map((platform, index) => (
            <div
              key={platform.name}
              className="group relative overflow-hidden rounded-2xl border border-gray-200 bg-white/80 p-8 backdrop-blur-sm transition-all hover:scale-105 hover:border-transparent hover:shadow-2xl"
              style={{ animationDelay: `${index * 100}ms` }}
            >
              <div className={`pointer-events-none absolute inset-0 bg-gradient-to-br ${platform.gradient} opacity-0 transition-opacity group-hover:opacity-10`} />
              <div className="relative flex flex-col items-center justify-center">
                <span className="mb-3 text-4xl transition-transform group-hover:scale-110">{platform.icon}</span>
                <span className="text-sm font-semibold text-gray-700">{platform.name}</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
