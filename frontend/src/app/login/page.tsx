'use client';

/**
 * Login Page - Premium Redesign
 *
 * Matches landing page design:
 * - MkDocs brand colors (Green #059669)
 * - Glassmorphism effects
 * - Gradient backgrounds
 * - Professional Supabase/Vercel style
 * Issue #223: i18n support
 */

import { useEffect, useState, Suspense } from 'react';
import { useTranslations } from 'next-intl';
import { useRouter, useSearchParams } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { getAuthUrl, getGitHubAuthUrl } from '@/lib/auth/auth';
import { apiClient } from '@/lib/api/base';
import { ArrowRight, AlertCircle, Sparkles, Shield, Zap } from 'lucide-react';
import { KaguraLogo } from '@/components/icons/KaguraLogo';
import { LanguageSelector } from '@/components/LanguageSelector';

function LoginContent() {
  const t = useTranslations('login');

  const router = useRouter();
  const searchParams = useSearchParams();
  const [loadingProvider, setLoadingProvider] = useState<'google' | 'github' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [agreedToTerms, setAgreedToTerms] = useState(false);
  const [enabledProviders, setEnabledProviders] = useState<string[]>(['google']);

  // Check if mock auth is enabled
  const isMockAuth =
    process.env.NODE_ENV === 'development' &&
    process.env.NEXT_PUBLIC_ENABLE_MOCK_AUTH === 'true';

  useEffect(() => {
    const errorParam = searchParams.get('error');
    if (errorParam === 'registration_disabled') {
      setError(t('registrationDisabled', { default: 'Registration is disabled. Please ask an admin for an invitation.' }));
    } else if (errorParam) {
      setError(decodeURIComponent(errorParam));
    }

    // Auto-redirect to dashboard if mock auth is enabled
    if (isMockAuth) {
      router.push('/workspace/contexts');
    }

  }, [searchParams, isMockAuth, router]);

  // Fetch enabled providers (Issue #360) — separate effect, runs once
  useEffect(() => {
    if (isMockAuth) return;
    apiClient.get<{ providers: { name: string }[] }>('/api/v1/auth/providers')
      .then(data => {
        const names = data.providers?.map(p => p.name) || [];
        if (names.length > 0) setEnabledProviders(names);
      })
      .catch(() => {}); // keep default ['google']
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleGoogleLogin = async () => {
    setLoadingProvider('google');
    setError(null);

    try {
      const authUrl = await getAuthUrl();
      window.location.href = authUrl;
    } catch (err) {
      setLoadingProvider(null);
      setError(err instanceof Error ? err.message : t('failedToLogin'));
    }
  };

  const handleGitHubLogin = async () => {
    setLoadingProvider('github');
    setError(null);

    try {
      const authUrl = await getGitHubAuthUrl();
      window.location.href = authUrl;
    } catch (err) {
      setLoadingProvider(null);
      setError(err instanceof Error ? err.message : t('failedToLogin'));
    }
  };

  // Show loading state while redirecting in mock auth mode
  if (isMockAuth) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-white">
        <div className="text-center">
          <div className="relative mx-auto mb-4">
            <div className="h-16 w-16 animate-spin rounded-full border-4 border-brand-green-200 border-t-brand-green-600" />
          </div>
          <p className="text-lg font-semibold text-gray-700">
            {t('mockAuthEnabled')}
          </p>
          <p className="text-sm text-gray-500 mt-2">
            {t('redirectingToDashboard')}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden bg-white">
      {/* Background Pattern */}
      <div className="absolute inset-0 -z-10 bg-[linear-gradient(to_right,#8080800a_1px,transparent_1px),linear-gradient(to_bottom,#8080800a_1px,transparent_1px)] bg-[size:14px_24px]" />
      <div className="absolute inset-0 -z-10 bg-gradient-to-b from-white via-gray-50/50 to-white" />

      {/* Gradient Orbs */}
      <div className="pointer-events-none absolute -left-1/4 -top-1/4 h-96 w-96 rounded-full bg-brand-green-300/30 blur-3xl" />
      <div className="pointer-events-none absolute -right-1/4 -bottom-1/4 h-96 w-96 rounded-full bg-emerald-300/30 blur-3xl" />

      {/* Language Selector - Top Right */}
      <div className="absolute top-4 right-4">
        <LanguageSelector />
      </div>

      <div className="relative w-full max-w-md px-4">
        {/* Logo */}
        <div className="mb-8 flex justify-center">
          <KaguraLogo className="h-24 w-auto" />
        </div>

        {/* Login Card with Glassmorphism */}
        <Card className="overflow-hidden border-gray-200 bg-white/80 shadow-2xl backdrop-blur-xl">
          <CardContent className="p-8">
            {/* Badge */}
            <div className="mb-6 flex justify-center">
              <div className="inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-brand-green-100 to-emerald-100 px-4 py-1.5 text-sm font-semibold text-brand-green-700">
                <Sparkles className="h-4 w-4" />
                <span>{t('welcomeToKagura')}</span>
              </div>
            </div>

            {/* Title */}
            <div className="mb-8 text-center">
              <h1 className="mb-2 text-3xl font-bold text-gray-900">
                {t('signInToAccount')}
              </h1>
              <p className="text-gray-600">
                {t('accessPlatform')}
              </p>
            </div>

            {/* Error Alert */}
            {error && (
              <Alert variant="destructive" className="mb-6 border-red-200 bg-red-50">
                <AlertCircle className="h-4 w-4" />
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            {/* Terms Agreement Checkbox */}
            <div className="mb-6">
              <label className="flex items-start gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={agreedToTerms}
                  onChange={(e) => setAgreedToTerms(e.target.checked)}
                  className="mt-1 h-4 w-4 rounded border-gray-300 text-brand-green-600 focus:ring-brand-green-500"
                />
                <span className="text-sm text-gray-700">
                  {t('agreeToTerms')}{' '}
                  <a href="/terms" target="_blank" className="font-medium text-brand-green-600 hover:underline">
                    {t('termsOfService')}
                  </a>{' '}
                  {t('termsAndPrivacy')}{' '}
                  <a href="/privacy" target="_blank" className="font-medium text-brand-green-600 hover:underline">
                    {t('privacyPolicy')}
                  </a>
                </span>
              </label>
            </div>

            {/* Google Sign-in Button (Issue #360: conditional) */}
            {enabledProviders.includes('google') && (
            <Button
              onClick={handleGoogleLogin}
              disabled={loadingProvider !== null || !agreedToTerms}
              size="lg"
              className="group relative h-14 w-full overflow-hidden bg-gradient-to-r from-brand-green-600 to-emerald-600 text-base font-semibold text-white shadow-xl shadow-brand-green-500/30 transition-all hover:scale-[1.02] hover:from-brand-green-700 hover:to-emerald-700 hover:shadow-2xl disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
            >
              <div className="pointer-events-none absolute inset-0 bg-gradient-to-r from-brand-green-700 to-emerald-700 opacity-0 transition-opacity group-hover:opacity-100" />

              {loadingProvider === 'google' ? (
                <span className="relative z-10 flex items-center justify-center gap-2">
                  <div className="h-5 w-5 animate-spin rounded-full border-2 border-white border-t-transparent" />
                  {t('redirectingToGoogle')}
                </span>
              ) : (
                <span className="relative z-10 flex items-center justify-center gap-2">
                  <svg className="h-5 w-5" viewBox="0 0 24 24">
                    <path fill="currentColor" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" />
                    <path fill="currentColor" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
                    <path fill="currentColor" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
                    <path fill="currentColor" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
                  </svg>
                  {t('continueWithGoogle')}
                  <ArrowRight className="ml-1 h-5 w-5 transition-transform group-hover:translate-x-1" />
                </span>
              )}
            </Button>
            )}

            {/* Divider — only when both providers enabled */}
            {enabledProviders.includes('google') && enabledProviders.includes('github') && (
            <div className="relative my-2">
              <div className="absolute inset-0 flex items-center">
                <span className="w-full border-t border-gray-300" />
              </div>
              <div className="relative flex justify-center text-xs uppercase">
                <span className="bg-white px-2 text-gray-500">or</span>
              </div>
            </div>
            )}

            {/* GitHub Sign-in Button (Issue #360: conditional) */}
            {enabledProviders.includes('github') && (
            <Button
              onClick={handleGitHubLogin}
              disabled={loadingProvider !== null || !agreedToTerms}
              size="lg"
              variant="outline"
              className="group relative h-14 w-full overflow-hidden text-base font-semibold shadow-md transition-all hover:scale-[1.02] hover:shadow-lg disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:scale-100"
            >
              {loadingProvider === 'github' ? (
                <span className="flex items-center justify-center gap-2">
                  <div className="h-5 w-5 animate-spin rounded-full border-2 border-gray-400 border-t-transparent" />
                  {t('redirecting', { default: 'Redirecting...' })}
                </span>
              ) : (
                <span className="flex items-center justify-center gap-2">
                  <svg className="h-5 w-5" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z" />
                  </svg>
                  {t('continueWithGitHub', { default: 'Continue with GitHub' })}
                  <ArrowRight className="ml-1 h-5 w-5 transition-transform group-hover:translate-x-1" />
                </span>
              )}
            </Button>
            )}

            {/* Features */}
            <div className="mt-8 space-y-3">
              {[
                { icon: Shield, text: t('secureOAuth') },
                { icon: Zap, text: t('instantAccess') },
                { icon: Sparkles, text: t('freeForever') },
              ].map((feature) => {
                const Icon = feature.icon;
                return (
                  <div key={feature.text} className="flex items-center gap-3 text-sm text-gray-700">
                    <div className="flex-shrink-0 rounded-lg bg-brand-green-100 p-2 text-brand-green-600">
                      <Icon className="h-4 w-4" />
                    </div>
                    <span>{feature.text}</span>
                  </div>
                );
              })}
            </div>

          </CardContent>
        </Card>

        {/* Back to Home */}
        <div className="mt-6 text-center">
          <button
            onClick={() => router.push('/')}
            className="text-sm font-medium text-gray-600 transition-colors hover:text-brand-green-600"
          >
            {t('backToHome')}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center bg-white">
          <div className="relative">
            <div className="h-16 w-16 animate-spin rounded-full border-4 border-brand-green-200 border-t-brand-green-600" />
            <div className="absolute inset-0 h-16 w-16 animate-ping rounded-full border-4 border-brand-green-600 opacity-20" />
          </div>
        </div>
      }
    >
      <LoginContent />
    </Suspense>
  );
}
