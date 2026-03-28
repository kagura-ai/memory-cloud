'use client';

import { Button } from '@/components/ui/button';
import {
  ArrowRight,
  Github,
  Terminal,
} from 'lucide-react';

export function GetStartedCTA() {
  return (
    <section className="relative overflow-hidden px-4 py-24 sm:px-6 sm:py-32 lg:px-8">
      <div className="absolute inset-0 bg-gradient-to-br from-brand-green-600 via-emerald-600 to-brand-green-700" />
      <div className="absolute inset-0 bg-[linear-gradient(to_right,#ffffff15_1px,transparent_1px),linear-gradient(to_bottom,#ffffff15_1px,transparent_1px)] bg-[size:40px_40px]" />

      <div className="absolute -left-20 -top-20 h-64 w-64 animate-blob rounded-full bg-white/10 mix-blend-overlay blur-3xl" />
      <div className="animation-delay-2000 absolute -right-20 -bottom-20 h-64 w-64 animate-blob rounded-full bg-white/10 mix-blend-overlay blur-3xl" />

      <div className="relative mx-auto max-w-3xl text-center">
        <div className="mb-6 inline-flex items-center gap-2 rounded-full border border-white/20 bg-white/10 px-4 py-2 text-sm font-semibold text-white backdrop-blur-sm">
          <Terminal className="h-4 w-4" />
          <span>Get started in minutes</span>
        </div>

        <h2 className="mb-6 text-4xl font-bold text-white sm:text-5xl">
          Deploy your own AI memory
        </h2>
        <p className="mb-10 text-xl leading-relaxed text-white/90">
          Self-hosted, open source, Apache 2.0 licensed.
          <br className="hidden sm:block" />
          Run with Docker Compose in 3 steps.
        </p>

        {/* Code block */}
        <div className="mx-auto mb-10 max-w-lg rounded-xl border border-white/20 bg-black/30 p-6 text-left backdrop-blur-sm">
          <div className="mb-3 flex items-center gap-2">
            <div className="h-3 w-3 rounded-full bg-red-400" />
            <div className="h-3 w-3 rounded-full bg-yellow-400" />
            <div className="h-3 w-3 rounded-full bg-green-400" />
          </div>
          <code className="block space-y-1 font-mono text-sm text-white/90">
            <span className="text-gray-400">$ </span>git clone https://github.com/kagura-ai/memory-cloud.git<br />
            <span className="text-gray-400">$ </span>cp .env.example .env.local<br />
            <span className="text-gray-400">$ </span>docker compose up -d
          </code>
        </div>

        <div className="flex flex-col items-center justify-center gap-4 sm:flex-row">
          <Button
            size="lg"
            asChild
            className="group h-14 bg-white px-8 text-base font-semibold text-brand-green-700 shadow-2xl transition-all hover:scale-105"
          >
            <a href="https://github.com/kagura-ai/memory-cloud" target="_blank" rel="noopener noreferrer">
              <Github className="mr-2 h-5 w-5" />
              View on GitHub
              <ArrowRight className="ml-2 h-5 w-5 transition-transform group-hover:translate-x-1" />
            </a>
          </Button>
        </div>

        <p className="mt-6 text-sm text-white/70">
          Apache License 2.0 — Free for commercial and personal use
        </p>
      </div>
    </section>
  );
}
