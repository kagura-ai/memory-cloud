'use client';

/**
 * Coming Soon Placeholder Component
 *
 * Issue #115 - Workspace-level Multi-tenancy Support
 * Displays a placeholder for features that are not yet implemented.
 */

import { Construction } from 'lucide-react';
import { PageContainer } from '@/components/common/PageContainer';
import { PageHeader } from '@/components/common/PageHeader';
import { Card, CardContent } from '@/components/ui/card';

interface ComingSoonProps {
  title: string;
  description: string;
  featureDescription?: string;
}

export function ComingSoon({ title, description, featureDescription }: ComingSoonProps) {
  return (
    <PageContainer>
      <PageHeader title={title} description={description} />

      <Card className="border-dashed border-2 border-slate-300 dark:border-slate-600 bg-slate-50 dark:bg-slate-800/50">
        <CardContent className="flex flex-col items-center justify-center py-16 text-center">
          <div className="rounded-full bg-slate-200 dark:bg-slate-700 p-4 mb-6">
            <Construction className="h-12 w-12 text-slate-500 dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-bold text-slate-700 dark:text-slate-200 mb-2">
            Coming Soon
          </h2>
          <p className="text-slate-500 dark:text-slate-400 max-w-md mb-4">
            This feature is currently under development and will be available in a future release.
          </p>
          {featureDescription && (
            <p className="text-sm text-slate-400 dark:text-slate-500 max-w-lg">
              {featureDescription}
            </p>
          )}
        </CardContent>
      </Card>
    </PageContainer>
  );
}
