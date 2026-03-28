'use client';

import { useTranslations } from 'next-intl';
import {
  ArrowRight,
  Check,
  Users,
} from 'lucide-react';

export function TeamCollabPreview() {
  const t = useTranslations('landing');

  return (
    <section className="relative overflow-hidden border-y border-gray-200/50 bg-white px-4 py-24 sm:px-6 sm:py-32 lg:px-8">
      <div className="mx-auto max-w-7xl">
        <div className="mb-16 text-center">
          <div className="mb-4 inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-purple-100 to-pink-100 px-4 py-1.5 text-sm font-semibold text-purple-700">
            <Users className="h-4 w-4" />
            <span>{t('teamCollabTitle')}</span>
          </div>
          <h2 className="mb-4 text-4xl font-bold text-gray-900 sm:text-5xl">{t('builtForTeams')}</h2>
          <p className="mx-auto max-w-2xl text-xl text-gray-600">
            {t('teamCollabDesc')}
          </p>
        </div>

        <div className="grid grid-cols-1 gap-8 md:grid-cols-3">
          {[
            {
              icon: '\u2709\uFE0F',
              title: t('teamInvitations'),
              description: t('teamInvitationsEnhanced'),
              gradient: 'from-purple-500 to-pink-500',
              features: [
                t('unlimitedInvitations'),
                t('emailRestrictions'),
                t('inAppNotifications'),
                t('expirationManagement'),
              ],
            },
            {
              icon: '\u{1F510}',
              title: t('roleBasedAccess'),
              description: t('rbacEnhanced'),
              gradient: 'from-blue-500 to-cyan-500',
              features: [
                t('ownerFullControl'),
                t('adminManageTeam'),
                t('memberReadWrite'),
                t('viewerReadOnly'),
              ],
            },
            {
              icon: '\u{1F512}',
              title: t('privacyControls'),
              description: t('privacyControlsDesc'),
              gradient: 'from-green-500 to-emerald-500',
              features: [
                t('privateByDefault'),
                t('sharedContextsIncluded'),
                t('contextMembersControl'),
                t('workspaceScopedKeys'),
              ],
            },
          ].map((feature, index) => {
            return (
              <div
                key={feature.title}
                className="group relative overflow-hidden rounded-3xl border border-gray-200 bg-gradient-to-br from-white to-gray-50 p-8 transition-all duration-500 hover:-translate-y-1 hover:border-transparent hover:shadow-2xl"
                style={{ animationDelay: `${index * 100}ms` }}
              >
                <div className={`pointer-events-none absolute inset-0 bg-gradient-to-br ${feature.gradient} opacity-0 transition-opacity duration-500 group-hover:opacity-5`} />

                <div className="relative">
                  <div className={`mb-6 inline-flex items-center justify-center w-14 h-14 rounded-2xl bg-gradient-to-br ${feature.gradient} text-white shadow-lg transition-transform group-hover:scale-110 text-2xl`}>
                    {feature.icon}
                  </div>

                  <h3 className="mb-3 text-xl font-bold text-gray-900">{feature.title}</h3>
                  <p className="mb-6 text-gray-600">{feature.description}</p>

                  <ul className="space-y-3">
                    {feature.features.map((item) => (
                      <li key={item} className="flex items-start text-sm text-gray-700">
                        <Check className="mr-2 mt-0.5 h-4 w-4 flex-shrink-0 text-brand-green-600" />
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            );
          })}
        </div>

        {/* CTA */}
        <div className="mt-12 text-center">
          <a
            href="/login"
            className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-purple-500 to-pink-600 px-8 py-3 text-base font-semibold text-white shadow-xl transition-all hover:from-purple-600 hover:to-pink-700 hover:shadow-2xl"
          >
            {t('getStarted')}
            <ArrowRight className="h-5 w-5" />
          </a>
        </div>
      </div>
    </section>
  );
}
