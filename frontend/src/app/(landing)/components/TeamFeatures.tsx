'use client';

import { useTranslations } from 'next-intl';
import { Button } from '@/components/ui/button';
import {
  ArrowRight,
  Check,
  Globe,
  Shield,
  GitBranch,
  Users,
} from 'lucide-react';

export function TeamFeatures() {
  const t = useTranslations('landing');

  return (
    <section id="team" className="relative overflow-hidden bg-white px-4 py-24 sm:px-6 sm:py-32 lg:px-8 border-y border-gray-200">
      <div className="mx-auto max-w-7xl">
        <div className="text-center mb-16">
          <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-orange-100 to-red-100 px-4 py-1.5 mb-4">
            <Users className="h-4 w-4 text-orange-600" />
            <span className="text-sm font-semibold text-orange-700">{t('teamCollabTitle')}</span>
          </div>
          <h2 className="text-4xl md:text-5xl font-bold text-gray-900 mb-4">
            {t('builtForTeams')}
          </h2>
          <p className="text-xl text-gray-600 max-w-3xl mx-auto">
            {t('teamSubtitle')}
          </p>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
          {/* Team Feature 1: Invitations */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <div className="rounded-xl bg-orange-600 p-3 text-white inline-flex mb-4">
              <GitBranch className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-gray-900 mb-3">{t('teamInvitations')}</h3>
            <p className="text-gray-600 mb-4 text-sm">
              {t('teamInvitationsDesc')}
            </p>
            <ul className="space-y-2 text-sm text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('emailInvitations')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('customExpiration')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('preassignRoles')}</span>
              </li>
            </ul>
          </div>

          {/* Team Feature 2: RBAC */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <div className="rounded-xl bg-red-600 p-3 text-white inline-flex mb-4">
              <Shield className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-gray-900 mb-3">{t('roleBasedAccess')}</h3>
            <p className="text-gray-600 mb-4 text-sm">
              {t('rbacDesc')}
            </p>
            <ul className="space-y-2 text-sm text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('ownerRole')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('adminRole')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('memberRole')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('viewerRole')}</span>
              </li>
            </ul>
          </div>

          {/* Team Feature 3: Shared Contexts */}
          <div className="rounded-3xl border border-gray-200 bg-white p-8">
            <div className="rounded-xl bg-pink-600 p-3 text-white inline-flex mb-4">
              <Globe className="h-6 w-6" />
            </div>
            <h3 className="text-xl font-bold text-gray-900 mb-3">{t('sharedContexts')}</h3>
            <p className="text-gray-600 mb-4 text-sm">
              {t('sharedContextsDesc')}
            </p>
            <ul className="space-y-2 text-sm text-gray-700">
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('privateSharedToggle')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('teamKnowledgeBase')}</span>
              </li>
              <li className="flex items-start gap-2">
                <Check className="h-4 w-4 text-brand-green-600 flex-shrink-0 mt-0.5" />
                <span>{t('controlledSharing')}</span>
              </li>
            </ul>
          </div>
        </div>

        {/* CTA */}
        <div className="mt-16 text-center">
          <Button size="lg" variant="default" asChild>
            <a href="/login">
              {t('startUsingTeams')}
              <ArrowRight className="ml-2 h-5 w-5" />
            </a>
          </Button>
        </div>
      </div>
    </section>
  );
}
