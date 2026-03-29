'use client';

import { KaguraLogo } from '@/components/icons/KaguraLogo';
import { Github } from 'lucide-react';

export function Footer() {
  return (
    <footer className="border-t border-gray-200 bg-gradient-to-b from-white to-gray-50 px-4 py-16 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-7xl">
        <div className="grid grid-cols-1 gap-12 md:grid-cols-4">
          <div className="md:col-span-1">
            <KaguraLogo className="mb-4 h-10 w-auto" />
            <p className="mb-4 text-sm text-gray-600">
              Universal AI Memory Platform
            </p>
            <a
              href="https://github.com/kagura-ai/memory-cloud"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 text-sm text-gray-600 transition-colors hover:text-brand-green-600"
            >
              <Github className="h-4 w-4" />
              Star on GitHub
            </a>
          </div>

          <div>
            <h3 className="mb-4 text-sm font-bold text-gray-900">Product</h3>
            <div className="space-y-3 text-sm">
              <div><a href="#features" className="text-gray-600 transition-colors hover:text-brand-green-600">Features</a></div>
              <div><a href="#setup" className="text-gray-600 transition-colors hover:text-brand-green-600">Setup Guide</a></div>
              <div><a href="/login" className="text-gray-600 transition-colors hover:text-brand-green-600">Sign In</a></div>
            </div>
          </div>

          <div>
            <h3 className="mb-4 text-sm font-bold text-gray-900">Resources</h3>
            <div className="space-y-3 text-sm">
              <div><a href="https://github.com/kagura-ai/memory-cloud" target="_blank" rel="noopener noreferrer" className="text-gray-600 transition-colors hover:text-brand-green-600">GitHub</a></div>
              <div><a href="https://github.com/kagura-ai/memory-cloud/blob/main/CONTRIBUTING.md" target="_blank" rel="noopener noreferrer" className="text-gray-600 transition-colors hover:text-brand-green-600">Contributing</a></div>
              <div><a href="https://github.com/kagura-ai/memory-cloud/issues" target="_blank" rel="noopener noreferrer" className="text-gray-600 transition-colors hover:text-brand-green-600">Issues</a></div>
            </div>
          </div>

          <div>
            <h3 className="mb-4 text-sm font-bold text-gray-900">Legal</h3>
            <div className="space-y-3 text-sm">
              <div><a href="https://github.com/kagura-ai/memory-cloud/blob/main/LICENSE" target="_blank" rel="noopener noreferrer" className="text-gray-600 transition-colors hover:text-brand-green-600">Apache License 2.0</a></div>
              <div><a href="https://github.com/kagura-ai/memory-cloud/blob/main/SECURITY.md" target="_blank" rel="noopener noreferrer" className="text-gray-600 transition-colors hover:text-brand-green-600">Security Policy</a></div>
            </div>
          </div>
        </div>

        <div className="mt-12 border-t border-gray-200 pt-8">
          <div className="flex flex-col items-center justify-between gap-4 sm:flex-row">
            <div className="flex items-center gap-2 text-sm text-gray-600">
              <span>&copy; 2024-2026 Kagura AI</span>
              <span className="text-gray-400">&bull;</span>
              <span>Open Source &bull; Apache 2.0</span>
            </div>
          </div>
        </div>
      </div>
    </footer>
  );
}
