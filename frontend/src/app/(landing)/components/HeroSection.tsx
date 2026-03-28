'use client';

import { useTranslations } from 'next-intl';
import { useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { KaguraLogo } from '@/components/icons/KaguraLogo';
import {
  ArrowRight,
  Check,
  Shield,
  Users,
  Github,
  Server,
} from 'lucide-react';

interface HeroSectionProps {
  mounted: boolean;
}

export function HeroSection({ mounted }: HeroSectionProps) {
  const t = useTranslations('landing');
  const router = useRouter();

  return (
    <section className={`relative px-4 py-24 sm:px-6 sm:py-32 lg:px-8 ${mounted ? 'animate-fade-in' : 'opacity-0'}`}>
      <div className="mx-auto max-w-4xl text-center">
        {/* Kagura Logo */}
        <div className="mb-4 sm:mb-8 flex justify-center">
          <KaguraLogo className="h-32 sm:h-48 md:h-60 w-auto" />
        </div>

        {/* Floating Badge */}
        <div className="mb-8 inline-flex animate-float items-center gap-2 rounded-full border border-brand-green-200 bg-gradient-to-r from-brand-green-50 to-emerald-50 px-5 py-2 shadow-lg shadow-brand-green-500/20 backdrop-blur-sm transition-all hover:scale-105 hover:shadow-xl hover:shadow-brand-green-500/30">
          <Shield className="h-4 w-4 text-brand-green-600" />
          <span className="bg-gradient-to-r from-brand-green-600 to-emerald-600 bg-clip-text text-sm font-semibold text-transparent">
            v0.9.0 - Secure + Team-Ready
          </span>
          <Users className="h-4 w-4 text-brand-green-600" />
        </div>

        {/* Main Headline with Gradient Animation */}
        <h1 className="mb-6 text-5xl font-bold leading-tight tracking-tight text-gray-900 sm:text-7xl sm:leading-tight">
          Never lose your AI context{' '}
          <span className="relative inline-block">
            <span className="animate-gradient bg-gradient-to-r from-brand-green-600 via-emerald-600 to-brand-green-600 bg-[length:200%_auto] bg-clip-text text-transparent">
              again
            </span>
            <svg className="absolute -bottom-2 left-0 w-full" height="8" viewBox="0 0 200 8" fill="none" xmlns="http://www.w3.workspace/2000/svg">
              <path d="M0 4C0 4 50 0 100 4C150 8 200 4 200 4" stroke="url(#gradient)" strokeWidth="3" strokeLinecap="round" className="animate-draw"/>
              <defs>
                <linearGradient id="gradient" x1="0%" y1="0%" x2="100%" y2="0%">
                  <stop offset="0%" stopColor="#059669"/>
                  <stop offset="100%" stopColor="#10b981"/>
                </linearGradient>
              </defs>
            </svg>
          </span>
        </h1>

        <p className="mb-4 text-xl font-semibold text-gray-800 sm:text-2xl">
          {t('heroSubtitle')}
        </p>

        <p className="mx-auto mb-10 max-w-2xl text-lg leading-relaxed text-gray-600">
          {t('heroDescription') || 'Enterprise-grade memory system with 3-layer architecture, Hybrid Search, and Neural Memory.'}
        </p>

        {/* CTA Buttons with Enhanced Effects */}
        <div className="flex flex-col items-center justify-center gap-4 sm:flex-row">
          <Button
            size="lg"
            onClick={() => router.push('/login')}
            className="group relative h-14 overflow-hidden bg-gradient-to-r from-brand-green-600 to-emerald-600 px-8 text-base font-semibold text-white shadow-2xl shadow-brand-green-500/50 transition-all hover:scale-105 hover:shadow-3xl hover:shadow-brand-green-500/60"
          >
            <div className="pointer-events-none absolute inset-0 bg-gradient-to-r from-brand-green-700 to-emerald-700 opacity-0 transition-opacity group-hover:opacity-100" />
            <span className="relative z-10 flex items-center">
              <svg className="mr-2 h-5 w-5" viewBox="0 0 24 24">
                <path fill="currentColor" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
                <path fill="currentColor" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
                <path fill="currentColor" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
                <path fill="currentColor" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
              </svg>
              {t('getStarted')}
              <ArrowRight className="ml-2 h-5 w-5 transition-transform group-hover:translate-x-1" />
            </span>
          </Button>

          <Button
            size="lg"
            variant="outline"
            asChild
            className="group h-14 border-2 border-gray-300 bg-white/50 px-8 text-base font-semibold backdrop-blur-sm transition-all hover:scale-105 hover:border-brand-green-600 hover:bg-white hover:shadow-xl"
          >
            <a href="https://github.com/kagura-ai/memory-cloud" target="_blank" rel="noopener noreferrer">
              <Github className="mr-2 h-5 w-5 text-gray-700 transition-transform group-hover:scale-110" />
              {t('viewOnGitHub')}
            </a>
          </Button>
        </div>

        {/* Trust Indicators with Icons */}
        <div className="mt-10 flex flex-wrap items-center justify-center gap-x-8 gap-y-3">
          {[
            { icon: Server, text: t('selfHosted'), color: 'text-brand-green-600' },
            { icon: Check, text: t('openSource'), color: 'text-yellow-600' },
            { icon: Shield, text: t('securePrivate'), color: 'text-blue-600' },
          ].map((item) => {
            const Icon = item.icon;
            return (
              <div key={item.text} className="flex items-center gap-2 text-sm font-medium text-gray-700">
                <div className={`rounded-full bg-opacity-10 p-1 ${item.color}`}>
                  <Icon className={`h-4 w-4 ${item.color}`} />
                </div>
                <span>{item.text}</span>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
