'use client';

import { useTranslations } from 'next-intl';
import { useRouter } from 'next/navigation';
import { LanguageSelector } from '@/components/LanguageSelector';
import { Button } from '@/components/ui/button';
import { KaguraIcon } from '@/components/icons/KaguraIcon';
import { Github } from 'lucide-react';

export function Header() {
  const t = useTranslations('landing');
  const router = useRouter();

  return (
    <nav className="sticky top-0 z-50 border-b border-white/20 bg-white/70 backdrop-blur-xl">
      <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-8">
        <div className="flex h-16 items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="relative">
              <KaguraIcon className="h-10 w-10" />
              <div className="absolute -right-1 -top-1 flex h-3 w-3">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand-green-400 opacity-75" />
                <span className="relative inline-flex h-3 w-3 rounded-full bg-brand-green-500" />
              </div>
            </div>
            <span className="text-sm font-medium text-gray-500">Open Source</span>
          </div>

          <div className="flex items-center gap-3">
            <Button
              variant="ghost"
              size="sm"
              asChild
              className="relative overflow-hidden transition-all hover:scale-105"
            >
              <a href="https://github.com/kagura-ai/memory-cloud#readme" target="_blank" rel="noopener noreferrer">
                {t('docs')}
              </a>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              asChild
              className="relative overflow-hidden transition-all hover:scale-105"
            >
              <a href="https://github.com/kagura-ai/memory-cloud" target="_blank" rel="noopener noreferrer">
                <Github className="mr-1 h-4 w-4" />
                GitHub
              </a>
            </Button>
            <LanguageSelector />
            <Button
              variant="ghost"
              size="sm"
              onClick={() => router.push('/login')}
              className="relative overflow-hidden transition-all hover:scale-105"
            >
              {t('signIn')}
            </Button>
          </div>
        </div>
      </div>
    </nav>
  );
}
