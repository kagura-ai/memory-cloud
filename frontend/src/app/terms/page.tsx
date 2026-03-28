'use client';

/**
 * Terms of Service Page - Kagura Memory Cloud
 *
 * GDPR-compliant terms of service
 */

import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { LanguageSelector } from '@/components/LanguageSelector';
import { Button } from '@/components/ui/button';
import { KaguraIcon } from '@/components/icons/KaguraIcon';
import { FileText, Shield, Users, AlertTriangle, CheckCircle, Scale, Globe } from 'lucide-react';

export default function TermsPage() {
  const t = useTranslations('terms');
  const router = useRouter();
  const lastUpdated = '2025-12-19';
  const effectiveDate = '2025-12-19';

  const sections = [
    {
      icon: FileText,
      title: t('sections.acceptance.title'),
      content: [
        t('sections.acceptance.intro'),
        '',
        t('sections.acceptance.eligibility'),
        '',
        t('sections.acceptance.account'),
        '',
        t('sections.acceptance.changes'),
      ].join('\n'),
    },
    {
      icon: CheckCircle,
      title: t('sections.serviceDescription.title'),
      content: [
        t('sections.serviceDescription.intro'),
        '',
        t('sections.serviceDescription.memoryStorage'),
        '',
        t('sections.serviceDescription.multiPlatform'),
        '',
        t('sections.serviceDescription.teamCollaboration'),
        '',
        t('sections.serviceDescription.apiAccess'),
      ].join('\n'),
    },
    {
      icon: Users,
      title: t('sections.userResponsibilities.title'),
      content: [
        t('sections.userResponsibilities.intro'),
        '',
        t('sections.userResponsibilities.lawfulUse'),
        '',
        t('sections.userResponsibilities.noAbuse'),
        '',
        t('sections.userResponsibilities.noSpam'),
        '',
        t('sections.userResponsibilities.accountSecurity'),
        '',
        t('sections.userResponsibilities.contentOwnership'),
      ].join('\n'),
    },
    {
      icon: Shield,
      title: t('sections.dataPrivacy.title'),
      content: [
        t('sections.dataPrivacy.intro'),
        '',
        t('sections.dataPrivacy.privacyPolicy'),
        '',
        t('sections.dataPrivacy.dataOwnership'),
        '',
        t('sections.dataPrivacy.dataProcessing'),
        '',
        t('sections.dataPrivacy.gdprRights'),
        '',
        t('sections.dataPrivacy.dataDeletion'),
      ].join('\n'),
    },
    {
      icon: Scale,
      title: t('sections.subscriptionBilling.title'),
      content: [
        t('sections.subscriptionBilling.intro'),
        '',
        t('sections.subscriptionBilling.freePlan'),
        '',
        t('sections.subscriptionBilling.paidPlans'),
        '',
        t('sections.subscriptionBilling.cancellation'),
        '',
        t('sections.subscriptionBilling.quotaChanges'),
      ].join('\n'),
    },
    {
      icon: AlertTriangle,
      title: t('sections.limitations.title'),
      content: [
        t('sections.limitations.intro'),
        '',
        t('sections.limitations.serviceAvailability'),
        '',
        t('sections.limitations.noWarranty'),
        '',
        t('sections.limitations.limitationOfLiability'),
        '',
        t('sections.limitations.dataLoss'),
        '',
        t('sections.limitations.thirdPartyServices'),
      ].join('\n'),
    },
    {
      icon: FileText,
      title: t('sections.termination.title'),
      content: [
        t('sections.termination.intro'),
        '',
        t('sections.termination.byYou'),
        '',
        t('sections.termination.byUs'),
        '',
        t('sections.termination.effectOfTermination'),
        '',
        t('sections.termination.survival'),
      ].join('\n'),
    },
    {
      icon: Globe,
      title: t('sections.governingLaw.title'),
      content: [
        t('sections.governingLaw.intro'),
        '',
        t('sections.governingLaw.governingLawContent'),
        '',
        t('sections.governingLaw.jurisdiction'),
        '',
        t('sections.governingLaw.euUsers'),
        '',
        t('sections.governingLaw.arbitration'),
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
              <Scale className="h-4 w-4" />
              <span>{t('header.badge')}</span>
            </div>
            <h1 className="mb-4 text-4xl font-bold text-gray-900 sm:text-5xl">
              {t('header.title')}
            </h1>
            <p className="text-xl text-gray-600">
              {t('header.subtitle')}
            </p>
            <div className="mt-6 flex items-center justify-center gap-4 text-sm text-gray-500">
              <span>{t('header.lastUpdated', { date: lastUpdated })}</span>
              <span>•</span>
              <span>{t('header.effectiveDate', { date: effectiveDate })}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="mx-auto max-w-4xl px-4 py-12 sm:px-6 lg:px-8">
        <div className="space-y-12">
          {sections.map((section, index) => {
            const Icon = section.icon;
            return (
              <div key={index} className="scroll-mt-24">
                <div className="mb-6 flex items-center gap-3">
                  <div className="rounded-xl bg-gradient-to-br from-brand-green-600 to-emerald-600 p-2 text-white">
                    <Icon className="h-5 w-5" />
                  </div>
                  <h2 className="text-2xl font-bold text-gray-900">{section.title}</h2>
                </div>
                <div className="prose prose-gray max-w-none">
                  {section.content.split('\n\n').map((paragraph, pIndex) => {
                    // Check if paragraph starts with **Header**
                    if (paragraph.startsWith('**') && paragraph.includes('**:')) {
                      const [header, ...rest] = paragraph.split('**:');
                      const headerText = header.replace(/\*\*/g, '');
                      const content = rest.join('**:');
                      return (
                        <div key={pIndex} className="mb-4">
                          <h3 className="mb-2 text-lg font-semibold text-gray-900">{headerText}</h3>
                          <p className="text-gray-700">{content}</p>
                        </div>
                      );
                    }
                    return (
                      <p key={pIndex} className="mb-4 text-gray-700">
                        {paragraph}
                      </p>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        {/* Contact */}
        <div className="mt-16 rounded-3xl border border-gray-200 bg-gradient-to-br from-gray-50 to-white p-8 text-center">
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

      {/* Footer */}
      <div className="border-t border-gray-200 bg-white py-8">
        <div className="mx-auto max-w-4xl px-4 sm:px-6 lg:px-8">
          <div className="flex flex-col items-center justify-between gap-4 text-sm text-gray-600 sm:flex-row">
            <span>{t('footer.copyright')}</span>
            <div className="flex gap-6">
              <a href="/privacy" className="transition-colors hover:text-brand-green-600">{t('footer.privacyPolicy')}</a>
              <a href="/terms" className="font-semibold text-brand-green-600">{t('footer.termsOfService')}</a>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
