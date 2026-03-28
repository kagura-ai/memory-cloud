'use client';

/**
 * Privacy Policy Page - Kagura Memory Cloud
 *
 * Privacy policy and data handling information
 */

import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { LanguageSelector } from '@/components/LanguageSelector';
import { Button } from '@/components/ui/button';
import { KaguraIcon } from '@/components/icons/KaguraIcon';
import { Shield, Lock, Eye, Database, FileText, Globe } from 'lucide-react';

export default function PrivacyPage() {
  const t = useTranslations('privacy');
  const router = useRouter();
  const lastUpdated = '2025-12-04';

  const sections = [
    {
      icon: Database,
      title: t('sections.dataCollection.title'),
      content: [
        t('sections.dataCollection.intro'),
        '',
        t('sections.dataCollection.accountInfo'),
        '',
        t('sections.dataCollection.memoryData'),
        '',
        t('sections.dataCollection.usageData'),
        '',
        t('sections.dataCollection.technicalData'),
      ].join('\n'),
    },
    {
      icon: Lock,
      title: t('sections.dataStorage.title'),
      content: [
        t('sections.dataStorage.intro'),
        '',
        t('sections.dataStorage.encryptionRest'),
        '',
        t('sections.dataStorage.encryptionTransit'),
        '',
        t('sections.dataStorage.accessControl'),
        '',
        t('sections.dataStorage.infrastructure'),
        '',
        t('sections.dataStorage.backups'),
      ].join('\n'),
    },
    {
      icon: Eye,
      title: t('sections.dataUsage.title'),
      content: [
        t('sections.dataUsage.intro'),
        '',
        t('sections.dataUsage.serviceProvision'),
        '',
        t('sections.dataUsage.serviceImprovement'),
        '',
        t('sections.dataUsage.support'),
        '',
        t('sections.dataUsage.security'),
        '',
        t('sections.dataUsage.weNever'),
      ].join('\n'),
    },
    {
      icon: Shield,
      title: t('sections.gdprRights.title'),
      content: [
        t('sections.gdprRights.intro'),
        '',
        t('sections.gdprRights.access'),
        '',
        t('sections.gdprRights.rectification'),
        '',
        t('sections.gdprRights.erasure'),
        '',
        t('sections.gdprRights.portability'),
        '',
        t('sections.gdprRights.restriction'),
        '',
        t('sections.gdprRights.objection'),
        '',
        t('sections.gdprRights.contact'),
      ].join('\n'),
    },
    {
      icon: Globe,
      title: t('sections.thirdParty.title'),
      content: [
        t('sections.thirdParty.intro'),
        '',
        t('sections.thirdParty.authentication'),
        '',
        t('sections.thirdParty.infrastructure'),
        '',
        '',
        t('sections.thirdParty.aiApis'),
        '',
        t('sections.thirdParty.conclusion'),
      ].join('\n'),
    },
    {
      icon: FileText,
      title: t('sections.dataRetention.title'),
      content: [
        t('sections.dataRetention.intro'),
        '',
        t('sections.dataRetention.activeAccounts'),
        '',
        t('sections.dataRetention.inactiveAccounts'),
        '',
        t('sections.dataRetention.deletedAccounts'),
        '',
        t('sections.dataRetention.backups'),
        '',
        t('sections.dataRetention.logs'),
      ].join('\n'),
    },
  ];

  return (
    <div className="min-h-screen bg-gradient-to-b from-gray-50 to-white">
      {/* Navigation */}
      <nav className="sticky top-0 z-50 border-b border-white/20 bg-white/70 backdrop-blur-xl">
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
          <div className="flex h-16 items-center justify-between">
            <div className="flex items-center">
              <div className="relative cursor-pointer" onClick={() => router.push('/')}>
                <KaguraIcon className="h-10 w-10" />
                <div className="absolute -right-1 -top-1 flex h-3 w-3">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-green-400 opacity-75" />
                  <span className="relative inline-flex h-3 w-3 rounded-full bg-brand-green-500" />
                </div>
              </div>
            </div>

            <div className="flex items-center gap-3">
              <LanguageSelector />
              <Button
                variant="ghost"
                size="sm"
                onClick={() => router.push('/login')}
                className="relative overflow-hidden transition-all hover:scale-105"
              >
                {t('header.signIn')}
              </Button>
              <Button
                size="sm"
                onClick={() => router.push('/login')}
                className="group relative overflow-hidden bg-gradient-to-r from-brand-green-600 to-emerald-600 transition-all hover:scale-105 hover:shadow-lg hover:shadow-brand-green-500/50"
              >
                <span className="relative z-10">{t('header.getStarted')}</span>
                <div className="pointer-events-none absolute inset-0 bg-gradient-to-r from-brand-green-700 to-emerald-700 opacity-0 transition-opacity group-hover:opacity-100" />
              </Button>
            </div>
          </div>
        </div>
      </nav>

      {/* Header */}
      <div className="border-b border-gray-200 bg-white">
        <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6 lg:px-8">
          <div className="text-center">
            <div className="mb-4 inline-flex items-center gap-2 rounded-full bg-blue-100 px-4 py-1.5 text-sm font-semibold text-blue-700">
              <Shield className="h-4 w-4" />
              <span>{t('header.badge')}</span>
            </div>
            <h1 className="mb-4 text-4xl font-bold text-gray-900 sm:text-5xl">
              {t('header.title')}
            </h1>
            <p className="mx-auto max-w-2xl text-lg text-gray-600">
              {t('header.subtitle')}
            </p>
            <p className="mt-4 text-sm text-gray-500">
              {t('header.lastUpdated', { date: lastUpdated })}
            </p>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="mx-auto max-w-4xl px-4 py-16 sm:px-6 lg:px-8">
        {/* Introduction */}
        <div className="mb-12 rounded-3xl border border-blue-200 bg-gradient-to-br from-blue-50 to-cyan-50 p-8">
          <h2 className="mb-4 text-2xl font-bold text-gray-900">{t('intro.title')}</h2>
          <p className="text-gray-700 leading-relaxed">
            {t('intro.paragraph1')}
          </p>
          <p className="mt-4 text-gray-700 leading-relaxed">
            {t('intro.paragraph2')}
          </p>
        </div>

        {/* Sections */}
        <div className="space-y-8">
          {sections.map((section, index) => {
            const Icon = section.icon;
            return (
              <div
                key={section.title}
                className="overflow-hidden rounded-3xl border border-gray-200 bg-white transition-all hover:shadow-lg"
                style={{ animationDelay: `${index * 100}ms` }}
              >
                <div className="p-8">
                  <div className="mb-4 flex items-center gap-4">
                    <div className="inline-flex rounded-2xl bg-gradient-to-br from-brand-green-500 to-emerald-600 p-3 text-white shadow-lg">
                      <Icon className="h-6 w-6" />
                    </div>
                    <h2 className="text-2xl font-bold text-gray-900">{section.title}</h2>
                  </div>
                  <div className="prose prose-gray max-w-none">
                    {section.content.split('\n\n').map((paragraph, idx) => {
                      if (paragraph.startsWith('**')) {
                        // Bold heading
                        const [heading, ...rest] = paragraph.split(':');
                        return (
                          <p key={idx} className="text-gray-700 leading-relaxed">
                            <strong className="font-semibold text-gray-900">
                              {heading.replace(/\*\*/g, '')}:
                            </strong>
                            {rest.join(':')}
                          </p>
                        );
                      }
                      return (
                        <p key={idx} className="text-gray-700 leading-relaxed">
                          {paragraph}
                        </p>
                      );
                    })}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* Additional Sections */}
        <div className="mt-12 space-y-8">
          {/* Cookies */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <h2 className="mb-4 text-2xl font-bold text-gray-900">{t('sections.cookies.title')}</h2>
            <p className="mb-4 text-gray-700 leading-relaxed">
              {t('sections.cookies.intro')}
            </p>
            <ul className="list-disc space-y-2 pl-6 text-gray-700">
              <li>{t('sections.cookies.sessionCookie')}</li>
            </ul>
            <p className="mt-4 text-gray-700 leading-relaxed">
              {t('sections.cookies.noTracking')}
            </p>
          </div>

          {/* Children's Privacy */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <h2 className="mb-4 text-2xl font-bold text-gray-900">{t('sections.childrenPrivacy.title')}</h2>
            <p className="text-gray-700 leading-relaxed">
              {t('sections.childrenPrivacy.content')}
            </p>
          </div>

          {/* Changes to Policy */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <h2 className="mb-4 text-2xl font-bold text-gray-900">{t('sections.policyChanges.title')}</h2>
            <p className="text-gray-700 leading-relaxed">
              {t('sections.policyChanges.intro')}
            </p>
            <ul className="list-disc space-y-2 pl-6 text-gray-700 mt-4">
              <li>{t('sections.policyChanges.emailNotification')}</li>
              <li>{t('sections.policyChanges.websiteNotice')}</li>
              <li>{t('sections.policyChanges.inAppNotification')}</li>
            </ul>
            <p className="mt-4 text-gray-700 leading-relaxed">
              {t('sections.policyChanges.acceptanceNote')}
            </p>
          </div>

          {/* Contact */}
          <div className="mt-12 rounded-3xl border border-gray-200 bg-gradient-to-br from-gray-50 to-white p-8 text-center">
            <h3 className="mb-4 text-xl font-bold text-gray-900">{t('contact.title')}</h3>
            <p className="mb-6 text-gray-600">
              {t('contact.subtitle')}
            </p>
            <div className="flex flex-col items-center gap-3 sm:flex-row sm:justify-center">
              <Button variant="outline" asChild>
                <a href="https://github.com/JFK" target="_blank" rel="noopener noreferrer">{t('contact.emailButton')}</a>
              </Button>
              <Button asChild>
                <a href="/">{t('contact.backToHome')}</a>
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* Footer */}
      <div className="border-t border-gray-200 bg-white py-8">
        <div className="mx-auto max-w-4xl px-4 sm:px-6 lg:px-8">
          <div className="flex flex-col items-center justify-between gap-4 text-sm text-gray-600 sm:flex-row">
            <span>{t('footer.copyright')}</span>
            <div className="flex gap-6">
              <a href="/privacy" className="font-semibold text-brand-green-600">{t('footer.privacyPolicy')}</a>
              <a href="/terms" className="transition-colors hover:text-brand-green-600">{t('footer.termsOfService')}</a>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
