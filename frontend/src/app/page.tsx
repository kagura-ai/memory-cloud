'use client';

/**
 * Kagura AI Landing Page - Modern Premium Design
 *
 * Features:
 * - Animated gradient backgrounds
 * - Glassmorphism effects
 * - Micro-interactions and hover states
 * - 3D card effects
 * - Scroll animations
 * - i18n support (Issue #223)
 */

import { useEffect, useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Header } from './(landing)/components/Header';
import { HeroSection } from './(landing)/components/HeroSection';
import { PlatformLogos } from './(landing)/components/PlatformLogos';
import { BenefitsSection } from './(landing)/components/BenefitsSection';
import { TeamCollabPreview } from './(landing)/components/TeamCollabPreview';
import { FeaturesGrid } from './(landing)/components/FeaturesGrid';
import { SetupGuide } from './(landing)/components/SetupGuide';
import { NeuralMemory } from './(landing)/components/NeuralMemory';
import { TeamFeatures } from './(landing)/components/TeamFeatures';
import { ResourceIngest } from './(landing)/components/ResourceIngest';
import { PublicContexts } from './(landing)/components/PublicContexts';
import { DocFreeDev } from './(landing)/components/DocFreeDev';
import { UseCases } from './(landing)/components/UseCases';
import { GetStartedCTA } from './(landing)/components/GetStartedCTA';
import { Footer } from './(landing)/components/Footer';

export default function LandingPage() {
  const { isLoading } = useAuth();
  const [mounted, setMounted] = useState(true);

  useEffect(() => {
    setMounted(true);
    // Allow logged-in users to view landing page (removed auto-redirect)
  }, []);

  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-gray-50 via-white to-gray-50">
        <div className="relative">
          <div className="h-16 w-16 animate-spin rounded-full border-4 border-gray-200 border-t-brand-green-600" />
          <div className="absolute inset-0 h-16 w-16 animate-ping rounded-full border-4 border-brand-green-600 opacity-20" />
        </div>
      </div>
    );
  }

  // Show landing page for all users (logged-in or not)
  return (
    <div className="relative min-h-screen overflow-hidden bg-white text-gray-900" style={{ '--foreground': '0 0% 3.9%', '--background': '0 0% 100%', '--accent': '0 0% 96.1%', '--accent-foreground': '0 0% 9%' } as React.CSSProperties}>
        {/* Animated Background Gradient Orbs */}
        <div className="pointer-events-none fixed inset-0 overflow-hidden">
          <div className="absolute -left-1/4 -top-1/4 h-[800px] w-[800px] animate-blob rounded-full bg-brand-green-300/30 mix-blend-multiply blur-3xl filter" />
          <div className="animation-delay-2000 absolute -right-1/4 -top-1/4 h-[800px] w-[800px] animate-blob rounded-full bg-emerald-300/30 mix-blend-multiply blur-3xl filter" />
          <div className="animation-delay-4000 absolute -bottom-1/4 left-1/2 h-[800px] w-[800px] animate-blob rounded-full bg-brand-green-400/20 mix-blend-multiply blur-3xl filter" />
        </div>

        <Header />
        <HeroSection mounted={mounted} />
        <PlatformLogos />
        <BenefitsSection />
        <TeamCollabPreview />
        <FeaturesGrid />
        <SetupGuide />
        <NeuralMemory />
        <TeamFeatures />
        <ResourceIngest />
        <PublicContexts />
        <DocFreeDev />
        <UseCases />
        <GetStartedCTA />
        <Footer />

        {/* Custom Animations CSS */}
        <style jsx global>{`
          @keyframes gradient {
            0%, 100% { background-position: 0% 50%; }
            50% { background-position: 100% 50%; }
          }

          @keyframes blob {
            0%, 100% { transform: translate(0, 0) scale(1); }
            33% { transform: translate(30px, -50px) scale(1.1); }
            66% { transform: translate(-20px, 20px) scale(0.9); }
          }

          @keyframes float {
            0%, 100% { transform: translateY(0px); }
            50% { transform: translateY(-10px); }
          }

          @keyframes draw {
            to { stroke-dashoffset: 0; }
          }

          @keyframes fade-in {
            from { opacity: 0; transform: translateY(20px); }
            to { opacity: 1; transform: translateY(0); }
          }

          @keyframes slideIn {
            from { opacity: 0; transform: translateX(-10px); }
            to { opacity: 1; transform: translateX(0); }
          }

          .animate-gradient { animation: gradient 3s ease infinite; }
          .animate-blob { animation: blob 7s ease-in-out infinite; }
          .animate-float { animation: float 3s ease-in-out infinite; }
          .animate-draw {
            stroke-dasharray: 200;
            stroke-dashoffset: 200;
            animation: draw 2s ease-in-out forwards;
          }
          .animate-fade-in { animation: fade-in 0.8s ease-out; }
          .animation-delay-2000 { animation-delay: 2s; }
          .animation-delay-4000 { animation-delay: 4s; }
        `}</style>
      </div>
    );
}
